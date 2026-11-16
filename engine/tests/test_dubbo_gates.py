"""Dubbo 执行门禁：Filter 是否在链上、序列化保真、mock_not_used。"""
from __future__ import annotations

from astsight.report import build_report


def _report(mock: dict) -> dict:
    return build_report(
        run_meta={"run_id": "r", "mode": "agent"},
        changes={},
        impact={"changed_apis": [], "unresolved_symbols": []},
        profiles=[],
        cases=[],
        records=[],
        unmapped={"symbols": [], "routes": [], "unsupported_files": []},
        route_audit={},
        codegen={},
        mock_evidence=mock,
    )


def _mock(**overrides) -> dict:
    base = {
        "checked": True,
        "calls": 0,
        "by_result": {},
        "plan": {"dubbo_route_count": 1},
        "agent": {"heartbeat": None, "reason": "agent_not_active"},
    }
    base.update(overrides)
    return base


def test_dubbo_plan_blocks_when_filter_not_in_chain() -> None:
    report = _report(_mock())
    assert report["gate"]["verdict"] == "fail"
    assert any("filter_not_in_chain" in item for item in report["gate"]["blockers"])


def test_dubbo_plan_blocks_when_filter_never_instantiated() -> None:
    heartbeat = {
        "sut_id": "sut",
        "dubbo": {
            "installed": True,
            "version": "2.7.22",
            "filter_instantiated": 0,
            "serialization_verified": False,
        },
    }
    report = _report(_mock(agent={"heartbeat": heartbeat, "reason": None}))
    assert any("filter_not_in_chain" in item for item in report["gate"]["blockers"])


def test_dubbo_heartbeat_reports_serialization_unverified() -> None:
    heartbeat = {
        "sut_id": "sut",
        "dubbo": {
            "installed": True,
            "version": "2.7.22",
            "filter_instantiated": 1,
            "filter_hits": 1,
            "serialization_verified": False,
        },
    }
    report = _report(_mock(agent={"heartbeat": heartbeat, "reason": None}))
    assert not any(
        "filter_not_in_chain" in item for item in report["gate"]["blockers"]
    )
    assert any(
        "serialization_verified=false" in item for item in report["gate"]["warnings"]
    )


def test_dubbo_bound_but_unused_still_blocks() -> None:
    heartbeat = {
        "sut_id": "sut",
        "dubbo": {
            "installed": True,
            "version": "2.7.22",
            "filter_instantiated": 1,
            "serialization_verified": False,
        },
    }
    report = _report(
        _mock(
            agent={"heartbeat": heartbeat, "reason": None},
            unused_binding_count=1,
            unused_bindings=[
                {
                    "case_id": "case-1",
                    "dep_id": "dubbo:com.acme.OrderService#create(java.lang.String)",
                    "branch": "success",
                    "session_id": "s1",
                }
            ],
        )
    )
    assert any("mock_not_used" in item for item in report["gate"]["blockers"])
