"""C7.4 审计完整性：weak_oracle、会话证据、audit 块与 token 汇总。"""
from __future__ import annotations

from pathlib import Path

from astsight.agent.flow import _usage_summary
from astsight.mock import MockEvidenceWriter, summarize_evidence
from astsight.report import build_report, render_markdown
from contracts import (
    AssertStrength,
    Assertion,
    MockCall,
    MockCallResult,
    Priority,
    TestCase,
)


def _case(
    *,
    case_id: str = "TC-0001",
    strength: AssertStrength = AssertStrength.L1,
    mock_bindings: dict[str, str] | None = None,
) -> TestCase:
    return TestCase(
        id=case_id,
        title="pay",
        endpoint="POST /pay",
        category="positive",
        method="POST",
        path="/pay",
        priority=Priority.P1,
        asserts=[Assertion(kind="status_code", op="eq", expect=200)],
        source="test",
        assert_strength=strength,
        mock_bindings=(
            mock_bindings
            if mock_bindings is not None
            else {"http:svc:pay POST /pay": "success"}
        ),
    )


def _mock_evidence(by_case: dict, *, sessions_checked: bool = True) -> dict:
    return {
        "checked": True,
        "by_case": by_case,
        "sessions_checked": sessions_checked,
        "session_evidence": [],
        "session_evidence_truncated": False,
        "degraded": False,
    }


def _report(case: TestCase, mock_evidence: dict) -> dict:
    return build_report(
        run_meta={"run_id": "r1", "mode": "cases", "project": "p"},
        changes={},
        impact={"changed_apis": []},
        profiles=[],
        cases=[case],
        records=[
            {
                "case_id": case.id,
                "endpoint": case.endpoint,
                "outcome": "passed",
                "attempts": [{"n": 1, "status": "pass"}],
                "diagnosis": {},
            }
        ],
        unmapped={},
        route_audit={},
        codegen={},
        mock_evidence=mock_evidence,
    )


def test_weak_oracle_marks_l1_case_without_mock_hits() -> None:
    case = _case()
    report = _report(case, _mock_evidence({}))
    assert report["summary"]["weak_oracle"] == [case.id]
    assert report["audit"]["weak_oracle"] == {"checked": True, "count": 1}
    assert any("weak_oracle" in item for item in report["gate"]["warnings"])
    markdown = render_markdown(report)
    assert "弱断言" in markdown
    assert "审计完整性" in markdown
    assert "egress_guard" in markdown


def test_non_l1_or_hit_case_is_not_weak_oracle() -> None:
    strong = _case(case_id="TC-0002", strength=AssertStrength.L2)
    report = _report(strong, _mock_evidence({}))
    assert report["summary"]["weak_oracle"] == []

    hit = _case(case_id="TC-0003")
    report = _report(
        hit,
        _mock_evidence({hit.id: {"calls": 1}}),
    )
    assert report["summary"]["weak_oracle"] == []


def test_audit_block_reports_unimplemented_items() -> None:
    report = _report(_case(), _mock_evidence({}))
    audit = report["audit"]
    assert audit["egress_guard"] == {"status": "not_implemented"}
    assert audit["transport_coverage"] == {"status": "not_implemented"}
    assert audit["transport_fallback"] == {"status": "not_applicable"}
    assert audit["instance"] == {"status": "not_applicable"}
    assert audit["token_usage"] == {"status": "not_available"}
    assert audit["mock_sessions"]["checked"] is True


def test_session_evidence_contains_binding_calls_and_status(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mock_calls.jsonl"
    writer = MockEvidenceWriter(path)
    writer.write(
        MockCall(
            call_id="c1",
            session_id="s1",
            case_id="TC-0001",
            dep_id="http:svc:pay POST /pay",
            route_key="r000000000001",
            route_match="method_path",
            method="POST",
            original_path="/pay",
            result=MockCallResult.OK,
            at="2026-11-02T00:00:00+00:00",
        )
    )
    sessions = [
        {
            "session_id": "s1",
            "case_id": "TC-0001",
            "sut_id": "sut",
            "namespace": "n",
            "active": False,
            "close_reason": "closed",
            "bindings": [
                {"dep_id": "http:svc:pay POST /pay", "branch": "success"},
                {"dep_id": "http:svc:notify POST /notify", "branch": "success"},
            ],
        },
        {
            "session_id": "s2",
            "case_id": "TC-0002",
            "sut_id": "sut",
            "namespace": "n",
            "active": True,
            "bindings": [],
        },
    ]
    summary = summarize_evidence(path, sessions=sessions)
    evidence = {item["session_id"]: item for item in summary["session_evidence"]}
    assert evidence["s1"]["case_id"] == "TC-0001"
    assert evidence["s1"]["status"] == "closed"
    calls = {item["dep_id"]: item["calls"] for item in evidence["s1"]["bindings"]}
    assert calls == {
        "http:svc:pay POST /pay": 1,
        "http:svc:notify POST /notify": 0,
    }
    assert evidence["s2"]["status"] == "open"
    assert summary["session_evidence_total"] == 2
    assert summary["session_evidence_truncated"] is False


def test_token_usage_summary_reports_totals_and_model() -> None:
    usage = _usage_summary(
        [
            {"input_tokens": 10, "output_tokens": 5, "prompt_chars": 100},
            {"input_tokens": None, "output_tokens": None, "prompt_chars": 50},
        ],
        "deepseek-v3",
    )
    assert usage["calls"] == 2
    assert usage["total_input_tokens"] == 10
    assert usage["total_output_tokens"] == 5
    assert usage["total_tokens"] == 15
    assert usage["by_model"]["deepseek-v3"]["calls"] == 2
    assert usage["by_node_status"] == "not_available"
