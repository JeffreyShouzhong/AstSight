"""gRPC Mock runtime adapter 与 gripmock REST backend 测试。"""
from __future__ import annotations

import json

import httpx
import pytest

from astsight.mock import (
    GripMockHttpBackend,
    GripMockProcess,
    GrpcMockRuntimeAdapter,
    GrpcMockRuntimeError,
)
from contracts import (
    GrpcMockLocator,
    GrpcMockReply,
    GrpcStatus,
    MockBinding,
    MockPlanDep,
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
