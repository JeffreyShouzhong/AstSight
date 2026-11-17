"""Dubbo 失败的确定性归因与按 transport 汇总。"""
from __future__ import annotations

from astsight.execute.diagnose import classify
from astsight.execute.runner import TestRecord
from astsight.report.merge import build_report
from contracts import TestCase, Transport


def _record(message: str) -> TestRecord:
    return TestRecord(
        nodeid="test_dubbo.py::T::test",
        case_id="TC-0001",
        classname="test_dubbo.T",
        name="test",
        outcome="failed",
        message=message,
    )


def test_dubbo_env_failures_are_retryable() -> None:
    timeout = classify(
        _record("expected rpc_status 'OK', got TIMEOUT: waiting for response"),
        None,
    )
    assert timeout["reason"] == "env_flake"
    assert timeout["retryable"] is True

    no_provider = classify(
        _record("unexpected rpc_status NO_PROVIDER"),
        None,
    )
    assert no_provider["reason"] == "env_flake"
    assert no_provider["retryable"] is True

    error_code = classify(
        _record("unexpected error_code: 'rpc_timeout'"),
        None,
    )
    assert error_code["reason"] == "env_flake"
    assert error_code["retryable"] is True


def test_dubbo_contract_failures_are_case_errors() -> None:
    bad_request = classify(
        _record("expected rpc_status 'OK', got BAD_REQUEST: param_types mismatch"),
        None,
    )
    assert bad_request["reason"] == "case_error"
    assert bad_request["root_cause"] == "case_error"
    assert bad_request["retryable"] is False

    serialization = classify(
        _record("unexpected error_code: 'serialization_error'"),
        None,
    )
    assert serialization["reason"] == "case_error"


def test_report_counts_by_transport() -> None:
    case = TestCase(
        id="TC-0001",
        transport=Transport.DUBBO,
        endpoint="dubbo:com.acme.OrderService#describe(java.lang.String)",
        category="positive",
        method="describe",
        path="",
        source="dubbo:test",
    )
    report = build_report(
        run_meta={"run_id": "r", "mode": "full"},
        changes={},
        impact={"changed_apis": [], "unresolved_symbols": []},
        profiles=[],
        cases=[case],
        records=[
            {
                "case_id": "TC-0001",
                "endpoint": case.endpoint,
                "category": "positive",
                "outcome": "passed",
                "attempts": [{"status": "pass"}],
                "diagnosis": {"reason": "passed"},
                "duration": 0.1,
                "message": "",
            }
        ],
        unmapped={"symbols": [], "routes": [], "unsupported_files": []},
        route_audit={},
        codegen={},
    )
    assert report["by_transport"]["dubbo"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "blocked": 0,
    }
