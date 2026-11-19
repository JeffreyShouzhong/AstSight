"""防止假绿：未执行/无 Mock 证据/未映射范围不能显示为通过。"""
from __future__ import annotations

from astsight.report import build_report, render_markdown
from contracts import (
    AssertStrength,
    Assertion,
    Priority,
    TestCase,
)


def _case(
    case_id: str = "TC-0001",
    *,
    strength: AssertStrength = AssertStrength.L1,
    mock_bindings: dict[str, str] | None = None,
) -> TestCase:
    return TestCase(
        id=case_id,
        title=case_id,
        endpoint="POST /orders",
        category="positive",
        method="POST",
        path="/orders",
        priority=Priority.P1,
        asserts=[Assertion(kind="status_code", op="eq", expect=200)],
        source="test",
        assert_strength=strength,
        mock_bindings=mock_bindings or {},
    )


def _record(case: TestCase, outcome: str = "passed") -> dict:
    return {
        "case_id": case.id,
        "endpoint": case.endpoint,
        "outcome": outcome,
        "attempts": [{"n": 1, "status": "pass" if outcome == "passed" else outcome}],
        "diagnosis": {},
    }


def _report(
    cases: list[TestCase],
    records: list[dict],
    *,
    mock_evidence: dict | None = None,
    unmapped: dict | None = None,
    profiles: list[dict] | None = None,
    allow_unmapped: bool = False,
) -> dict:
    return build_report(
        run_meta={"run_id": "r1", "mode": "cases", "project": "p"},
        changes={},
        impact={"changed_apis": []},
        profiles=profiles or [],
        cases=cases,
        records=records,
        unmapped=unmapped or {},
        route_audit={},
        codegen={},
        gate_config={"allow_unmapped": allow_unmapped},
        mock_evidence=mock_evidence,
    )


def test_all_skipped_is_not_run_not_pass() -> None:
    case = _case()
    report = _report([case], [_record(case, "skipped")])
    assert report["gate"]["verdict"] == "not_run"
    assert report["summary"]["executed_cases"] == 0
    assert report["summary"]["unexecuted_cases"] == 1
    assert report["failures"] == []
    assert any("未执行" in item for item in report["gate"]["warnings"])


def test_skipped_is_not_counted_as_failure() -> None:
    passed = _case("TC-0001")
    skipped = _case("TC-0002")
    report = _report(
        [passed, skipped],
        [_record(passed), _record(skipped, "skipped")],
    )
    assert report["summary"]["executed_cases"] == 1
    assert report["summary"]["unexecuted_cases"] == 1
    assert report["summary"]["final_pass"] == 1
    assert report["failures"] == []
    assert report["gate"]["verdict"] == "pass"


def test_mock_bound_without_evidence_is_weak_oracle() -> None:
    case = _case(mock_bindings={"http:svc:pay POST /pay": "success"})
    report = _report(
        [case],
        [_record(case)],
        mock_evidence={
            "checked": False,
            "reason": "mock_evidence_not_configured",
        },
    )
    assert report["summary"]["weak_oracle"] == [case.id]
    assert any(
        "Mock 证据未配置" in item for item in report["gate"]["warnings"]
    )
    assert "审计完整性" in render_markdown(report)


def test_unmapped_scope_blocks_pass() -> None:
    case = _case()
    report = _report(
        [case],
        [_record(case)],
        unmapped={"symbols": ["OrderService.create"]},
    )
    assert report["gate"]["verdict"] == "fail"
    assert any("unmapped" in item for item in report["gate"]["blockers"])


def test_incomplete_contract_blocks_pass() -> None:
    case = _case()
    report = _report(
        [case],
        [_record(case)],
        profiles=[
            {
                "endpoint": "POST /orders",
                "contract_incomplete": True,
            }
        ],
    )
    assert report["gate"]["verdict"] == "fail"
    assert any("unmapped" in item for item in report["gate"]["blockers"])
