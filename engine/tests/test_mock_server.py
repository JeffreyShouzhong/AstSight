from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from astsight.mock import (
    CallSite,
    MockHttpServer,
    MockLimits,
    assign_route_keys,
    build_dep_id_plan,
)
from contracts import MockRoute, MockStub

TOKEN = "test-admin-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}


def _server(tmp_path: Path, **kwargs) -> tuple[MockHttpServer, str, str]:
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
    keys = assign_route_keys(plan.deps)
    route_key = keys[dep.dep_id]
    server = MockHttpServer(
        sut_id="sut1",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
        **kwargs,
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={
            "success": MockStub(
                body={
                    "code": 0,
                    "trans_no": "T{run_scope}_{case_id}_PAY",
                }
            ),
            "insufficient_balance": MockStub(
                status=400, body={"code": "40001", "msg": "insufficient"}
            ),
        },
    )
    return server, dep.dep_id, route_key


def _run(coro):
    return asyncio.run(coro)


def _session_body(dep_id: str, branch: str = "success", **extra) -> dict:
    payload = {
        "session_id": "s1",
        "namespace": "sut1",
        "case_id": "TC-B-001",
        "run_scope": "R1",
        "bindings": [{"dep_id": dep_id, "branch": branch}],
    }
    payload.update(extra)
    return payload


async def _call(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    return await client.request(method, url, **kwargs)


def test_host_route_uses_session_branch_and_renders_variables(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            create = await _call(
                client, "POST", "/_admin/sessions", headers=ADMIN, json=_session_body(dep_id)
            )
            assert create.status_code == 200
            response = await _call(
                client,
                "POST",
                "/gateway/pay?trace=1",
                headers={
                    "Host": "pay-service",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer secret",
                },
                content=b'{"amount": 10}',
            )
            assert response.status_code == 200
            assert response.json() == {"code": 0, "trans_no": "TR1_TC-B-001_PAY"}
            return response

    _run(scenario())
    calls = server.evidence.records
    assert len(calls) == 1
    call = calls[0]
    assert call.result == "ok"
    assert call.route_match == "host_path"
    assert call.dep_id == dep_id
    assert call.branch == "success"
    assert call.query_keys == ["trace"]
    assert call.request_headers["authorization"] == "<redacted>"
    assert call.request_headers["content-type"] == "application/json"
    assert call.body_bytes == 14


def test_direct_route_key_wins_and_second_case_switches_branch(tmp_path: Path) -> None:
    server, dep_id, route_key = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client, "POST", "/_admin/sessions", headers=ADMIN, json=_session_body(dep_id)
            )
            first = await _call(client, "POST", f"/_dep/{route_key}/gateway/pay")
            assert first.status_code == 200
            assert first.json()["trans_no"].endswith("_PAY")

            await _call(client, "DELETE", "/_admin/sessions/s1", headers=ADMIN)
            second_body = _session_body(dep_id, "insufficient_balance", session_id="s2")
            await _call(client, "POST", "/_admin/sessions", headers=ADMIN, json=second_body)
            second = await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            assert second.status_code == 400
            assert second.json()["code"] == "40001"

    _run(scenario())
    assert [call.route_match for call in server.evidence.records] == [
        "path_prefix",
        "host_path",
    ]


def test_unknown_and_ambiguous_routes_are_not_passed_through(tmp_path: Path) -> None:
    server = MockHttpServer(sut_id="sut1", token=TOKEN)
    server.add_route(
        MockRoute(
            route_key="r000000000001",
            dep_id="http:svc:a GET /same",
            method="GET",
            path_key="/same",
            host="a",
        ),
        {"ok": MockStub(body={"from": "a"})},
    )
    server.add_route(
        MockRoute(
            route_key="r000000000002",
            dep_id="http:svc:b GET /same",
            method="GET",
            path_key="/same",
            host="b",
        ),
        {"ok": MockStub(body={"from": "b"})},
    )

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ambiguous = await _call(client, "GET", "/same", headers={"Host": "unknown"})
            assert ambiguous.status_code == 502
            assert ambiguous.json()["error"] == "mock_route_ambiguous"
            unknown = await _call(client, "GET", "/missing", headers={"Host": "unknown"})
            assert unknown.status_code == 502
            assert unknown.json()["error"] == "mock_route_unknown"
            # 直接 route_key 未知也不透传
            unknown_key = await _call(client, "GET", "/_dep/rffffffffffff/x")
            assert unknown_key.status_code == 502
            assert unknown_key.json()["error"] == "mock_route_unknown"

    _run(scenario())
    assert [call.result for call in server.evidence.records] == ["ambiguous", "unknown", "unknown"]


def test_path_template_matches_and_global_unique_falls_back(tmp_path: Path) -> None:
    server = MockHttpServer(sut_id="sut1", token=TOKEN)
    server.add_route(
        MockRoute(
            route_key="r000000000001",
            dep_id="http:svc:order-service GET /orders/{}",
            method="GET",
            path_key="/orders/{orderId}",
            host="order-service",
        ),
        {"success": MockStub(body={"order_id": 1})},
    )

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body("http:svc:order-service GET /orders/{}"),
            )
            by_host = await _call(
                client, "GET", "/orders/123", headers={"Host": "order-service"}
            )
            assert by_host.status_code == 200
            by_method = await _call(client, "GET", "/orders/456", headers={"Host": "unknown"})
            assert by_method.status_code == 200

    _run(scenario())
    assert [call.route_match for call in server.evidence.records] == [
        "host_path",
        "method_path",
    ]


def test_session_errors_are_explicit(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            assert missing.status_code == 502
            assert missing.json()["error"] == "mock_session_missing"

            duplicate = await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, duplicate=True)
                | {
                    "bindings": [
                        {"dep_id": dep_id, "branch": "success"},
                        {"dep_id": dep_id, "branch": "insufficient_balance"},
                    ]
                },
            )
            assert duplicate.status_code == 400
            assert duplicate.json()["error"] == "mock_binding_duplicate"

            wrong_ns = await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, namespace="other"),
            )
            assert wrong_ns.status_code == 409
            assert wrong_ns.json()["error"] == "mock_identity_mismatch"

            created = await _call(
                client, "POST", "/_admin/sessions", headers=ADMIN, json=_session_body(dep_id)
            )
            assert created.status_code == 200
            conflict = await _call(
                client, "POST", "/_admin/sessions", headers=ADMIN, json=_session_body(dep_id)
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"] == "mock_session_conflict"

            no_branch = await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, "missing"),
            )
            assert no_branch.status_code == 409  # active session 仍在

            await _call(client, "DELETE", "/_admin/sessions/s1", headers=ADMIN)
            late = await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            assert late.status_code == 502
            assert late.json()["error"] == "mock_late_call"

    _run(scenario())


def test_branch_missing_and_admin_auth(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = await _call(client, "GET", "/_admin/health")
            assert unauthorized.status_code == 401
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, "missing"),
            )
            response = await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            assert response.status_code == 502
            assert response.json()["error"] == "mock_branch_not_found"

    _run(scenario())
    assert server.evidence.records[0].result == "branch_missing"


def test_invalid_stub_headers_and_limits(tmp_path: Path) -> None:
    server, dep_id, route_key = _server(
        tmp_path,
        limits=MockLimits(request_body_bytes=4, request_headers_bytes=250, drop_timeout_ms=5),
    )
    server.route_table.get_by_dep(dep_id).branches["bad_header"] = MockStub(
        headers={"Connection": "close"}
    )

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id),
            )
            too_large = await _call(
                client,
                "POST",
                "/gateway/pay",
                headers={"Host": "pay-service"},
                content=b"12345",
            )
            assert too_large.status_code == 413
            too_many_headers = await _call(
                client,
                "POST",
                "/gateway/pay",
                headers={
                    "Host": "pay-service",
                    "X-Filler": "x" * 200,
                },
            )
            assert too_many_headers.status_code == 413

            await _call(client, "DELETE", "/_admin/sessions/s1", headers=ADMIN)
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, "bad_header", session_id="s2"),
            )
            bad = await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            assert bad.status_code == 502
            assert bad.json()["error"] == "mock_response_invalid"

    _run(scenario())


def test_drop_connection_is_best_effort_timeout(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path, limits=MockLimits(drop_timeout_ms=5))
    server.route_table.get_by_dep(dep_id).branches["drop"] = MockStub(drop_connection=True)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, "drop"),
            )
            response = await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            assert response.status_code == 502
            assert response.json()["error"] == "mock_connection_dropped"

    _run(scenario())
    call = server.evidence.records[-1]
    assert call.drop_mode == "timeout"
    assert call.result == "ok"


def test_drain_waits_for_inflight_and_reports_timeout(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)
    server.route_table.get_by_dep(dep_id).branches["slow"] = MockStub(
        body={"ok": True}, delay_ms=200
    )

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, "slow"),
            )
            pending = asyncio.create_task(
                _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            )
            for _ in range(100):
                if server._inflight_count():
                    break
                await asyncio.sleep(0.005)
            assert server._inflight_count() == 1
            drained = await _call(
                client,
                "POST",
                "/_admin/sessions/s1/drain",
                headers=ADMIN,
                json={"timeout_ms": 1000},
            )
            assert drained.status_code == 200
            assert drained.json()["drained"] is True
            assert drained.json()["inflight"] == 0
            response = await pending
            assert response.status_code == 200

            await _call(client, "DELETE", "/_admin/sessions/s1", headers=ADMIN)
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, "slow", session_id="s2"),
            )
            slow = asyncio.create_task(
                _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            )
            for _ in range(100):
                if server._inflight_count():
                    break
                await asyncio.sleep(0.005)
            short = await _call(
                client,
                "POST",
                "/_admin/sessions/s2/drain",
                headers=ADMIN,
                json={"timeout_ms": 10},
            )
            assert short.status_code == 200
            assert short.json()["drained"] is False
            assert short.json()["inflight"] == 1
            await slow

    _run(scenario())


def test_route_error_attributed_to_active_session(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id),
            )
            unknown = await _call(
                client, "POST", "/missing", headers={"Host": "pay-service"}
            )
            assert unknown.status_code == 502
            assert unknown.json()["error"] == "mock_route_unknown"

    _run(scenario())
    call = server.evidence.records[-1]
    assert call.result == "unknown"
    assert call.case_id == "TC-B-001"
    assert call.session_id == "s1"


def test_late_call_attributed_to_closed_session(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id),
            )
            await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            await _call(client, "DELETE", "/_admin/sessions/s1", headers=ADMIN)
            late = await _call(
                client, "POST", "/gateway/pay", headers={"Host": "pay-service"}
            )
            assert late.status_code == 502
            assert late.json()["error"] == "mock_late_call"

    _run(scenario())
    call = server.evidence.records[-1]
    assert call.result == "late"
    assert call.session_id == "s1"
    assert call.case_id == "TC-B-001"
    assert call.branch is None


def test_session_history_filters_run_scope_and_keeps_bindings(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id),
            )
            await _call(client, "POST", "/gateway/pay", headers={"Host": "pay-service"})
            await _call(client, "DELETE", "/_admin/sessions/s1", headers=ADMIN)
            await _call(
                client,
                "POST",
                "/_admin/sessions",
                headers=ADMIN,
                json=_session_body(dep_id, session_id="s2", run_scope="R2"),
            )

            r1 = await _call(
                client,
                "GET",
                "/_admin/sessions",
                headers=ADMIN,
                params={"run_scope": "R1"},
            )
            assert r1.status_code == 200
            payload = r1.json()
            assert payload["count"] == 1
            assert payload["sessions"][0]["session_id"] == "s1"
            assert payload["sessions"][0]["active"] is False
            assert payload["sessions"][0]["bindings"] == [
                {"dep_id": dep_id, "branch": "success"}
            ]

            r2 = await _call(
                client,
                "GET",
                "/_admin/sessions",
                headers=ADMIN,
                params={"run_scope": "R2"},
            )
            assert [item["session_id"] for item in r2.json()["sessions"]] == ["s2"]
            assert r2.json()["sessions"][0]["active"] is True

            invalid = await _call(
                client,
                "GET",
                "/_admin/sessions",
                headers=ADMIN,
                params={"limit": "0"},
            )
            assert invalid.status_code == 400

    _run(scenario())


def test_drain_rejects_unknown_session(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await _call(
                client,
                "POST",
                "/_admin/sessions/nope/drain",
                headers=ADMIN,
                json={"timeout_ms": 10},
            )
            assert missing.status_code == 404
            assert missing.json()["error"] == "mock_session_not_found"

    _run(scenario())


def test_real_uvicorn_lifecycle(tmp_path: Path) -> None:
    server, dep_id, _ = _server(tmp_path)
    server.start()
    try:
        assert server.started
        assert ":0" not in server.endpoint
        health = httpx.get(f"{server.endpoint}/_admin/health", headers=ADMIN, timeout=5)
        assert health.status_code == 200
        info = httpx.get(f"{server.endpoint}/_admin/server", headers=ADMIN, timeout=5)
        assert info.status_code == 200
        assert info.json()["history_path"] == str(tmp_path / "mock_calls.jsonl")
        assert info.json()["route_count"] == 1
        session = httpx.post(
            f"{server.endpoint}/_admin/sessions",
            headers=ADMIN,
            json=_session_body(dep_id),
            timeout=5,
        )
        assert session.status_code == 200
        response = httpx.post(
            f"{server.endpoint}/gateway/pay",
            headers={"Host": "pay-service"},
            timeout=5,
        )
        assert response.status_code == 200
        assert response.json()["trans_no"] == "TR1_TC-B-001_PAY"
    finally:
        server.stop()
    assert server.started is False
