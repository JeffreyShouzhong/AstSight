"""Dubbo 执行门禁：Filter 是否在链上、序列化保真、mock_not_used。"""
from __future__ import annotations

from types import SimpleNamespace

from astsight import pipeline
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


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        mock_admin_url="http://127.0.0.1:1", mock_token=None
    )


def _heartbeat(instantiated: int, *, installed: bool = True) -> dict:
    return {
        "sut_id": "sut",
        "dubbo": {
            "installed": installed,
            "version": "2.7.22",
            "filter_instantiated": instantiated,
            "serialization_verified": False,
        },
    }


def test_heartbeat_waits_for_filter_instantiation_when_dubbo_planned(
    monkeypatch,
) -> None:
    """Filter 可能在上一拍心跳之后才进链；有限轮询能读到新值。"""
    stale = _heartbeat(0)
    fresh = _heartbeat(1)
    replies = [(stale, None), (fresh, None)]
    monkeypatch.setattr(pipeline, "_HEARTBEAT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(
        pipeline, "_read_agent_heartbeat", lambda _config: replies.pop(0)
    )
    payload, reason = pipeline._fetch_agent_heartbeat(
        _config(), wait_for_dubbo_filter=True
    )
    assert payload == fresh
    assert reason is None


def test_heartbeat_does_not_wait_without_dubbo_plan(monkeypatch) -> None:
    stale = _heartbeat(0)
    monkeypatch.setattr(
        pipeline, "_read_agent_heartbeat", lambda _config: (stale, None)
    )
    payload, reason = pipeline._fetch_agent_heartbeat(_config())
    assert payload == stale
    assert reason is None


def test_heartbeat_gives_up_when_agent_disappears(monkeypatch) -> None:
    stale = _heartbeat(0)
    replies = [(stale, None), (None, "agent_not_active")]
    monkeypatch.setattr(pipeline, "_HEARTBEAT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(
        pipeline, "_read_agent_heartbeat", lambda _config: replies.pop(0)
    )
    payload, reason = pipeline._fetch_agent_heartbeat(
        _config(), wait_for_dubbo_filter=True
    )
    assert payload is None
    assert reason == "agent_not_active"


def test_heartbeat_does_not_wait_when_dubbo_module_missing(monkeypatch) -> None:
    missing = _heartbeat(0, installed=False)
    monkeypatch.setattr(
        pipeline, "_read_agent_heartbeat", lambda _config: (missing, None)
    )
    payload, reason = pipeline._fetch_agent_heartbeat(
        _config(), wait_for_dubbo_filter=True
    )
    assert payload == missing
    assert reason is None


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
