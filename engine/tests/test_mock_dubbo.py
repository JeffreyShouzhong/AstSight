"""Dubbo 取桩入口（`/_admin/dubbo/invoke`）的单元测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from astsight.mock import MockHttpServer
from contracts import MockStub

TOKEN = "dubbo-admin-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}
DEP_ID = "dubbo:com.acme.OrderService#create(java.lang.String)?group=order&version=1.0.0"


def _server(tmp_path: Path) -> MockHttpServer:
    server = MockHttpServer(
        sut_id="sut-dubbo",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dubbo_dep(
        DEP_ID,
        branches={
            "success": MockStub(body={"orderId": "O-1", "amount": 10}),
            "empty": MockStub(body=None),
        },
    )
    return server


def _session(server: MockHttpServer, *, bindings: list[dict]) -> None:
    response = _run(
        _call(
            server,
            "POST",
            "/_admin/sessions",
            json={
                "session_id": "session-1",
                "namespace": "sut-dubbo",
                "case_id": "case-1",
                "ttl_ms": 60_000,
                "bindings": bindings,
            },
        )
    )
    assert response.status_code == 200, response.text


def _run(coro):
    return asyncio.run(coro)


async def _call(server: MockHttpServer, method: str, url: str, **kwargs):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, headers=ADMIN, **kwargs)


def _invoke(server: MockHttpServer, dep_id: str = DEP_ID, method: str = "create"):
    return _run(
        _call(
            server,
            "POST",
            "/_admin/dubbo/invoke",
            json={
                "sut_id": "sut-dubbo",
                "dep_id": dep_id,
                "service": "com.acme.OrderService",
                "method": method,
                "parameter_types": ["java.lang.String"],
                "generic": False,
            },
        )
    )


def test_dubbo_stub_returns_branch_body_and_records_evidence(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _session(server, bindings=[{"dep_id": DEP_ID, "branch": "success"}])

    response = _invoke(server)
    assert response.status_code == 200
    payload = response.json()
    assert payload["dep_id"] == DEP_ID
    assert payload["branch"] == "success"
    assert payload["body"] == {"orderId": "O-1", "amount": 10}
    assert payload["route_key"].startswith("r")

    calls = server.evidence.records
    assert len(calls) == 1
    call = calls[0]
    assert call.dep_id == DEP_ID
    assert call.adapter == "dubbo-filter"
    assert call.mode == "agent_filter"
    assert call.method == "create"
    assert call.original_path == "dubbo:com.acme.OrderService#create"
    assert call.branch == "success"
    assert call.result.value == "ok"
    assert call.session_id == "session-1"
    assert call.case_id == "case-1"

    routes = _run(_call(server, "GET", "/_admin/agent/routes")).json()
    assert DEP_ID in routes["dubbo_deps"]


def test_dubbo_empty_body_is_null_not_error(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _session(server, bindings=[{"dep_id": DEP_ID, "branch": "empty"}])

    response = _invoke(server)
    assert response.status_code == 200
    assert response.json()["body"] is None


def test_dubbo_unbound_dep_fails_closed(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _session(
        server,
        bindings=[{"dep_id": "dubbo:com.acme.OtherService#other(java.lang.String)", "branch": "success"}],
    )

    response = _invoke(server)
    assert response.status_code == 502
    assert response.json()["error"] == "mock_unexpected_dep"
    call = server.evidence.records[0]
    assert call.result.value == "unbound"
    assert call.error == "mock_unexpected_dep"


def test_dubbo_missing_branch_fails_closed(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _session(server, bindings=[{"dep_id": DEP_ID, "branch": "missing"}])

    response = _invoke(server)
    assert response.status_code == 502
    assert response.json()["error"] == "mock_branch_not_found"
    assert server.evidence.records[0].result.value == "branch_missing"


def test_dubbo_missing_session_fails_closed(tmp_path: Path) -> None:
    server = _server(tmp_path)

    response = _invoke(server)
    assert response.status_code == 502
    assert response.json()["error"] == "mock_session_missing"
    assert server.evidence.records[0].result.value == "unbound"


def test_dubbo_unplanned_dep_is_explicit(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _session(server, bindings=[{"dep_id": DEP_ID, "branch": "success"}])

    response = _invoke(server, dep_id="dubbo:com.acme.Unknown#calc(java.lang.String)")
    assert response.status_code == 404
    assert response.json()["error"] == "mock_dep_unplanned"
    call = server.evidence.records[0]
    assert call.dep_id == "dubbo:com.acme.Unknown#calc(java.lang.String)"
    assert call.result.value == "unknown"


def test_dubbo_raw_base64_stub_is_invalid(tmp_path: Path) -> None:
    server = MockHttpServer(sut_id="sut-dubbo", token=TOKEN)
    server.add_dubbo_dep(
        DEP_ID, branches={"success": MockStub(body={"raw_base64": "AAEC"})}
    )
    _session(server, bindings=[{"dep_id": DEP_ID, "branch": "success"}])

    response = _invoke(server)
    assert response.status_code == 502
    assert response.json()["error"] == "mock_response_invalid"


def test_add_dubbo_dep_requires_parameter_signature(tmp_path: Path) -> None:
    import pytest
    from astsight.mock import DepIdError

    server = MockHttpServer(sut_id="sut-dubbo", token=TOKEN)
    with pytest.raises(DepIdError):
        server.add_dubbo_dep("dubbo:com.acme.OrderService#create")


def test_dubbo_overloads_are_distinct_routes(tmp_path: Path) -> None:
    first = "dubbo:com.acme.OrderService#create(java.lang.String)"
    second = "dubbo:com.acme.OrderService#create(java.lang.String,int)"
    server = MockHttpServer(
        sut_id="sut-dubbo",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dubbo_dep(first, branches={"success": MockStub(body={"n": 1})})
    server.add_dubbo_dep(second, branches={"success": MockStub(body={"n": 2})})
    routes = _run(_call(server, "GET", "/_admin/agent/routes")).json()
    assert routes["dubbo_deps"] == sorted([first, second])
