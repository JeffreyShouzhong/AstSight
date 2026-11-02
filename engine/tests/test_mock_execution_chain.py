from __future__ import annotations

from pathlib import Path

from astsight.cases.draft import (
    DraftCase,
    DraftRequirementRef,
    case_fingerprint,
    to_test_case,
)
from astsight.codegen import generate_pytest
from astsight.execute.runner import run_pytest
from astsight.mock import (
    CallSite,
    MockEvidenceWriter,
    MockHttpServer,
    assign_route_keys,
    build_dep_id_plan,
    summarize_evidence,
)
from astsight.report import build_report, render_markdown
from contracts import (
    Assertion,
    MockCall,
    MockCallResult,
    MockStub,
    TestCase as CaseModel,
)

TOKEN = "test-admin-token"


def _mock_call(
    call_id: str,
    *,
    case_id: str | None,
    result: MockCallResult,
    error: str | None = None,
    session_id: str | None = "s1",
) -> MockCall:
    return MockCall(
        call_id=call_id,
        session_id=session_id,
        case_id=case_id,
        dep_id="http:svc:pay POST /pay",
        route_key="r000000000001",
        route_match="method_path",
        method="POST",
        original_path="/pay",
        result=result,
        error=error,
        at="2026-11-02T00:00:00+00:00",
    )


def test_evidence_summary_feeds_report_gate_and_markdown(tmp_path: Path) -> None:
    path = tmp_path / "mock_calls.jsonl"
    writer = MockEvidenceWriter(path)
    writer.write(_mock_call("c1", case_id="TC-0001", result=MockCallResult.OK))
    writer.write(
        _mock_call(
            "c2",
            case_id="TC-0001",
            result=MockCallResult.LATE,
            error="mock_late_call",
        )
    )
    writer.write(
        _mock_call(
            "c3",
            case_id="TC-0002",
            result=MockCallResult.UNKNOWN,
            error="mock_route_unknown",
            session_id=None,
        )
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")

    summary = summarize_evidence(path)
    assert summary["checked"] is True
    assert summary["calls"] == 3
    assert summary["invalid_lines"] == 1
    assert summary["by_result"] == {"late": 1, "ok": 1, "unknown": 1}
    assert summary["blocking_errors"] == 1
    assert len(summary["late_calls"]) == 1
    assert summary["by_case"]["TC-0001"]["calls"] == 2
    assert summary["degraded"] is True

    report = build_report(
        run_meta={"run_id": "r1", "mode": "cases", "project": "p"},
        changes={},
        impact={"changed_apis": []},
        profiles=[],
        cases=[],
        records=[],
        unmapped={},
        route_audit={},
        codegen={},
        mock_evidence=summary,
    )
    assert any("阻断类错误" in item for item in report["gate"]["blockers"])
    assert any("晚到/过期调用" in item for item in report["gate"]["warnings"])
    markdown = render_markdown(report)
    assert "## 下游 Mock" in markdown
    assert "晚到调用" in markdown


def test_unused_bound_dependency_blocks_report(tmp_path: Path) -> None:
    path = tmp_path / "mock_calls.jsonl"
    writer = MockEvidenceWriter(path)
    writer.write(_mock_call("c1", case_id="TC-0001", result=MockCallResult.OK))

    sessions = [
        {
            "session_id": "s1",
            "case_id": "TC-0001",
            "bindings": [
                {"dep_id": "http:svc:pay POST /pay", "branch": "success"},
                {"dep_id": "http:svc:ledger POST /ledger", "branch": "success"},
            ],
        },
        {
            "session_id": "s2",
            "case_id": "TC-0002",
            "bindings": [
                {"dep_id": "http:svc:notify POST /notify", "branch": "success"},
            ],
        },
    ]
    summary = summarize_evidence(path, sessions=sessions)
    assert summary["sessions_checked"] is True
    assert summary["session_records"] == 2
    assert summary["unused_binding_count"] == 2
    assert {item["dep_id"] for item in summary["unused_bindings"]} == {
        "http:svc:ledger POST /ledger",
        "http:svc:notify POST /notify",
    }
    assert summary["degraded"] is True

    report = build_report(
        run_meta={"run_id": "r1", "mode": "cases", "project": "p"},
        changes={},
        impact={"changed_apis": []},
        profiles=[],
        cases=[],
        records=[],
        unmapped={},
        route_audit={},
        codegen={},
        mock_evidence=summary,
    )
    assert any("mock_not_used" in item for item in report["gate"]["blockers"])
    markdown = render_markdown(report)
    assert "绑定但零调用" in markdown
    assert "http:svc:notify POST /notify" in markdown


def test_evidence_sessions_missing_from_history_degrade_report(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mock_calls.jsonl"
    writer = MockEvidenceWriter(path)
    writer.write(_mock_call("c1", case_id="TC-0001", result=MockCallResult.OK))

    summary = summarize_evidence(
        path,
        sessions=[
            {
                "session_id": "other",
                "case_id": "TC-0002",
                "bindings": [],
            }
        ],
    )
    assert summary["sessions_missing"] == 1
    assert summary["session_ids_missing"] == ["s1"]
    assert summary["degraded"] is True

    report = build_report(
        run_meta={"run_id": "r1", "mode": "cases", "project": "p"},
        changes={},
        impact={"changed_apis": []},
        profiles=[],
        cases=[],
        records=[],
        unmapped={},
        route_audit={},
        codegen={},
        mock_evidence=summary,
    )
    assert any(
        "不在会话历史中" in item for item in report["gate"]["warnings"]
    )


def test_draft_bindings_flow_into_test_case_and_fingerprint() -> None:
    draft = DraftCase(
        case_key="pay_success",
        requirement_refs=[
            DraftRequirementRef(doc="api.md", section="支付", quote="支付成功")
        ],
        endpoint="POST /gateway/pay",
        category="positive",
        asserts=[Assertion(kind="status_code", op="eq", expect=200)],
        mock_bindings={"http:svc:pay-service POST /gateway/pay": "success"},
    )
    case = to_test_case(
        draft,
        method="POST",
        path="/gateway/pay",
        doc_versions={"api.md": "sha256:x"},
        generator={"kind": "manual"},
    )
    assert case.mock_bindings == draft.mock_bindings

    changed = case.model_copy(update={"mock_bindings": {}})
    assert case_fingerprint(case, draft.case_key) != case_fingerprint(
        changed, draft.case_key
    )


def test_generated_case_creates_and_closes_mock_session(
    tmp_path: Path, monkeypatch
) -> None:
    plan = build_dep_id_plan(
        [
            CallSite(
                "pay-call",
                "POST",
                "http://pay-service/gateway/pay",
                service_name="pay-service",
            )
        ]
    )
    dep = plan.deps[0]
    route_key = assign_route_keys(plan.deps)[dep.dep_id]
    server = MockHttpServer(
        sut_id="sut1",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={"success": MockStub(body={"code": 0, "msg": "ok"})},
    )

    case = CaseModel(
        id="TC-0001",
        endpoint="POST /gateway/pay",
        category="positive",
        method="POST",
        path="/gateway/pay",
        asserts=[
            Assertion(kind="status_code", op="eq", expect=200),
            Assertion(kind="body_field", target="code", op="eq", expect=0),
        ],
        source="test",
        mock_bindings={dep.dep_id: "success"},
    )
    generated = tmp_path / "generated"
    result = generate_pytest([case], generated)
    assert not result.errors
    conftest = (generated / "conftest.py").read_text(encoding="utf-8")
    assert "def _open_mock_session" in conftest
    test_file = next(generated.glob("test_*.py"))
    assert "with mock_session('TC-0001'" in test_file.read_text(encoding="utf-8")

    for name in (
        "ASTSIGHT_MOCK_ADMIN_URL",
        "ASTSIGHT_MOCK_TOKEN",
        "ASTSIGHT_MOCK_NAMESPACE",
        "ASTSIGHT_MOCK_DRAIN_MS",
    ):
        monkeypatch.delenv(name, raising=False)

    server.start()
    try:
        execution = run_pytest(
            generated,
            base_url=server.endpoint,
            junit_path=tmp_path / "results.xml",
            timeout=120,
            mock_admin_url=server.endpoint,
            mock_token=TOKEN,
            mock_namespace="sut1",
            mock_drain_ms=500,
        )
    finally:
        server.stop()

    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert [record.outcome for record in execution.records] == ["passed"]
    assert server.sessions.current() is None

    calls = server.evidence.records
    assert len(calls) == 1
    call = calls[0]
    assert call.result == "ok"
    assert call.case_id == "TC-0001"
    assert call.session_id
    assert call.dep_id == dep.dep_id
    assert call.branch == "success"
    assert call.route_match == "method_path"

    summary = summarize_evidence(
        server.evidence.path, sessions=server.sessions.history()
    )
    assert summary["sessions_checked"] is True
    assert summary["session_records"] == 1
    assert summary["unused_binding_count"] == 0
    assert summary["degraded"] is False
