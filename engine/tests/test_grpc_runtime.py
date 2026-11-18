"""gRPC Mock runtime adapter 与 gripmock REST backend 测试。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from astsight.mock import (
    GripMockHttpBackend,
    GripMockProcess,
    GrpcMockRuntimeAdapter,
    GrpcMockRuntimeError,
    start_mock_plan,
)
from contracts import (
    GrpcMockLocator,
    GrpcMockReply,
    GrpcStatus,
    MockBinding,
    MockPlanDep,
    MockPlan,
    MockSession,
    MockStub,
    Transport,
)


class FakeBackend:
    def __init__(self) -> None:
        self.cleared_stubs = 0
        self.cleared_history = 0
        self.installed: list[dict] = []
        self.records: list[dict] = []

    def health(self) -> None:
        return None

    def clear_stubs(self) -> None:
        self.cleared_stubs += 1

    def clear_history(self) -> None:
        self.cleared_history += 1
        self.records = []

    def install_stubs(self, stubs: list[dict]) -> list[str]:
        self.installed = stubs
        return [f"stub-{index + 1}" for index in range(len(stubs))]

    def history(self, *, limit: int = 10_000) -> list[dict]:
        return list(self.records)


def _dep() -> MockPlanDep:
    return MockPlanDep(
        dep_id="grpc:/inventory.v1.InventoryService/Reserve",
        transport=Transport.GRPC,
        locator=GrpcMockLocator(
            grpc_path="/inventory.v1.InventoryService/Reserve",
        ),
        branches={
            "success": MockStub(body={"available": True}),
            "unavailable": MockStub(body={}),
        },
        replies={
            "success": GrpcMockReply(body={"available": True}),
            "unavailable": GrpcMockReply(
                grpc_status=GrpcStatus.UNAVAILABLE,
                message="inventory offline",
            ),
        },
    )


def _session(branch: str = "success") -> MockSession:
    return MockSession(
        session_id="S-1",
        namespace="sut",
        case_id="TC-1",
        sut_id="sut",
        bindings=[
            MockBinding(
                dep_id="grpc:/inventory.v1.InventoryService/Reserve",
                branch=branch,
            )
        ],
        created_at="2026-11-18T00:00:00+00:00",
        expires_at="2026-11-18T00:01:00+00:00",
    )


def test_runtime_installs_session_stub_and_normalizes_history() -> None:
    backend = FakeBackend()
    adapter = GrpcMockRuntimeAdapter(backend, [_dep()])
    session = _session()

    adapter.start()
    adapter.activate(session)
    assert backend.cleared_stubs == 1
    assert backend.cleared_history == 1
    assert backend.installed[0]["service"] == "inventory.v1.InventoryService"
    assert backend.installed[0]["method"] == "Reserve"
    assert backend.installed[0]["output"]["code"] == 0

    backend.records = [
        {
            "service": "inventory.v1.InventoryService",
            "method": "Reserve",
            "stubId": "stub-1",
            "code": 0,
            "requests": [{"sku": "SKU-1"}],
            "responses": [{"available": True}],
            "elapsedMs": 3,
            "timestamp": "2026-11-18T00:00:01Z",
        }
    ]
    calls = adapter.collect(session)
    adapter.deactivate(session)

    assert backend.cleared_stubs == 2
    assert len(calls) == 1
    call = calls[0]
    assert call.transport is Transport.GRPC
    assert call.dep_id == "grpc:/inventory.v1.InventoryService/Reserve"
    assert call.branch == "success"
    assert call.grpc_status is GrpcStatus.OK
    assert call.result.value == "ok"
    assert call.adapter == "gripmock"
    assert call.request_body is None


def test_unmatched_grpc_call_is_unbound_evidence() -> None:
    backend = FakeBackend()
    adapter = GrpcMockRuntimeAdapter(backend, [_dep()])
    session = _session()
    adapter.activate(session)
    backend.records = [
        {
            "service": "inventory.v1.InventoryService",
            "method": "Reserve",
            "code": 5,
            "requests": [],
            "responses": [],
            "timestamp": "2026-11-18T00:00:01Z",
        }
    ]

    call = adapter.collect(session)[0]

    assert call.result.value == "unbound"
    assert call.error == "mock_unexpected_dep"
    assert call.grpc_status is GrpcStatus.NOT_FOUND


def test_error_branch_maps_canonical_status_and_message() -> None:
    backend = FakeBackend()
    adapter = GrpcMockRuntimeAdapter(backend, [_dep()])
    adapter.activate(_session("unavailable"))

    stub = backend.installed[0]

    assert stub["output"]["code"] == 14
    assert stub["output"]["error"] == "inventory offline"


def test_gripmock_http_backend_uses_official_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/health/readiness":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/api/stubs" and request.method == "POST":
            return httpx.Response(200, json=["stub-1"])
        if request.url.path == "/api/history":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = GripMockHttpBackend("http://gripmock", client=client)

    backend.health()
    backend.clear_history()
    ids = backend.install_stubs([{"service": "S", "method": "M"}])
    backend.history()
    backend.clear_stubs()

    assert ids == ["stub-1"]
    assert seen == [
        ("GET", "/api/health/readiness"),
        ("DELETE", "/api/history"),
        ("POST", "/api/stubs"),
        ("GET", "/api/history"),
        ("DELETE", "/api/stubs"),
    ]


def test_gripmock_process_uses_allocated_ports_and_missing_binary_is_explicit(
    tmp_path,
) -> None:
    descriptor = tmp_path / "service.proto"
    descriptor.write_text('syntax = "proto3";', encoding="utf-8")
    process = GripMockProcess(
        executable=tmp_path / "missing-gripmock",
        descriptor_source=descriptor,
        grpc_port=41001,
        http_port=41002,
        gateway_port=41003,
        work_dir=tmp_path,
    )

    assert process.command() == [
        str((tmp_path / "missing-gripmock").resolve()),
        str(descriptor.resolve()),
    ]
    assert process.environment()["GRPC_PORT"] == "41001"
    assert process.environment()["HTTP_PORT"] == "41002"

    with pytest.raises(GrpcMockRuntimeError) as error:
        process.start()
    assert error.value.code == "mock_binary_missing"


def test_grpc_plan_session_lifecycle_installs_collects_and_clears(
    tmp_path: Path,
) -> None:
    (tmp_path / "inventory.proto").write_text(
        """
        syntax = "proto3";
        package inventory.v1;
        message ReserveRequest { string sku = 1; }
        message ReserveResponse { bool available = 1; }
        service InventoryService {
          rpc Reserve(ReserveRequest) returns (ReserveResponse);
        }
        """,
        encoding="utf-8",
    )
    plan = MockPlan(
        sut_id="sut",
        deps=[
            MockPlanDep(
                dep_id="grpc:/inventory.v1.InventoryService/Reserve",
                transport=Transport.GRPC,
                locator=GrpcMockLocator(
                    grpc_path="/inventory.v1.InventoryService/Reserve",
                    descriptor_source="inventory.proto",
                ),
                branches={"success": MockStub(body={"available": True})},
                replies={"success": GrpcMockReply(body={"available": True})},
            )
        ],
    )
    plan_path = tmp_path / "grpc-plan.json"
    plan_path.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    backend = FakeBackend()
    runtime = start_mock_plan(
        plan_path,
        history_path=tmp_path / "mock_calls.jsonl",
        grpc_backend=backend,
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=runtime.server.app)
        headers = {"Authorization": f"Bearer {runtime.token}"}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/_admin/sessions",
                headers=headers,
                json={
                    "session_id": "S-1",
                    "namespace": "sut",
                    "case_id": "TC-1",
                    "bindings": [
                        {
                            "dep_id": "grpc:/inventory.v1.InventoryService/Reserve",
                            "branch": "success",
                        }
                    ],
                },
            )
            assert created.status_code == 200, created.text
            assert len(backend.installed) == 1
            backend.records = [
                {
                    "service": "inventory.v1.InventoryService",
                    "method": "Reserve",
                    "stubId": "stub-1",
                    "code": 0,
                    "elapsedMs": 2,
                    "timestamp": "2026-11-18T00:00:01Z",
                }
            ]
            drained = await client.post(
                "/_admin/sessions/S-1/drain",
                headers=headers,
                json={"timeout_ms": 20},
            )
            assert drained.status_code == 200, drained.text
            deleted = await client.delete(
                "/_admin/sessions/S-1",
                headers=headers,
            )
            assert deleted.status_code == 200, deleted.text

    try:
        asyncio.run(scenario())
        assert [call.transport for call in runtime.server.evidence.records] == [
            Transport.GRPC
        ]
        assert runtime.server.evidence.records[0].branch == "success"
        assert backend.cleared_stubs >= 2
    finally:
        runtime.stop()


def test_grpc_plan_starts_process_factory_and_exposes_endpoints(
    tmp_path: Path,
) -> None:
    (tmp_path / "inventory.proto").write_text(
        """
        syntax = "proto3";
        package inventory.v1;
        message ReserveRequest { string sku = 1; }
        message ReserveResponse { bool available = 1; }
        service InventoryService {
          rpc Reserve(ReserveRequest) returns (ReserveResponse);
        }
        """,
        encoding="utf-8",
    )
    plan = MockPlan(
        sut_id="sut",
        deps=[
            MockPlanDep(
                dep_id="grpc:/inventory.v1.InventoryService/Reserve",
                transport=Transport.GRPC,
                locator=GrpcMockLocator(
                    grpc_path="/inventory.v1.InventoryService/Reserve",
                    descriptor_source="inventory.proto",
                ),
                branches={"success": MockStub(body={"available": True})},
                replies={"success": GrpcMockReply(body={"available": True})},
            )
        ],
    )
    plan_path = tmp_path / "grpc-process-plan.json"
    plan_path.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    created: dict = {}

    class FakeProcess:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.backend = FakeBackend()
            self.started = 0
            self.stopped = 0
            created.update(kwargs)

        def start(self) -> None:
            self.started += 1

        def stop(self) -> None:
            self.stopped += 1

    process = None

    def factory(**kwargs):
        nonlocal process
        process = FakeProcess(**kwargs)
        return process

    runtime = start_mock_plan(
        plan_path,
        gripmock_path=tmp_path / "gripmock",
        grpc_process_factory=factory,
        grpc_ports=(41001, 41002, 41003),
    )
    try:
        assert process is not None
        assert process.started == 1
        assert created["grpc_port"] == 41001
        assert runtime.grpc_endpoint == "127.0.0.1:41001"
        assert runtime.grpc_admin_endpoint == "http://127.0.0.1:41002"
        assert runtime.grpc_gateway_endpoint == "127.0.0.1:41003"
        manifest = runtime.manifest(run_id="run-1")
        assert manifest["grpc_endpoint"] == "127.0.0.1:41001"
        injection = runtime.injection_document()["injections"][0]
        assert injection["strategy"] == "grpc_server"
        assert injection["injected"] == "127.0.0.1:41001"
    finally:
        runtime.stop()
    assert process.stopped == 1
