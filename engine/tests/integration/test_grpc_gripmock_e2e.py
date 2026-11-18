"""真实 gripmock binary 的 unary Mock 闭环。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import grpc
import httpx
import pytest
from google.protobuf import json_format

from astsight.mock import GrpcDescriptorIndex, start_mock_plan
from contracts import (
    GrpcMockLocator,
    GrpcMockReply,
    GrpcStatus,
    MockPlan,
    MockPlanDep,
    MockStub,
    Transport,
)

GRIPMOCK_BIN = os.environ.get("GRIPMOCK_BIN", "").strip()
pytestmark = pytest.mark.skipif(
    not GRIPMOCK_BIN or not Path(GRIPMOCK_BIN).is_file(),
    reason="set GRIPMOCK_BIN to a real gripmock binary",
)

PROTO = """
syntax = "proto3";
package inventory.v1;

message ReserveRequest { string sku = 1; }
message ReserveResponse {
  bool available = 1;
  string reservation_id = 2;
}

service InventoryService {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
"""
GRPC_PATH = "/inventory.v1.InventoryService/Reserve"
DEP_ID = f"grpc:{GRPC_PATH}"


def _plan(tmp_path: Path) -> Path:
    proto = tmp_path / "inventory.proto"
    proto.write_text(PROTO, encoding="utf-8")
    plan = MockPlan(
        sut_id="gripmock-e2e",
        deps=[
            MockPlanDep(
                dep_id=DEP_ID,
                transport=Transport.GRPC,
                locator=GrpcMockLocator(
                    grpc_path=GRPC_PATH,
                    descriptor_source="inventory.proto",
                    property="INVENTORY_GRPC_ADDR",
                ),
                branches={
                    "success": MockStub(
                        body={"available": True, "reservation_id": "R-1"}
                    ),
                    "unavailable": MockStub(body=None),
                },
                replies={
                    "success": GrpcMockReply(
                        body={"available": True, "reservation_id": "R-1"}
                    ),
                    "unavailable": GrpcMockReply(
                        grpc_status=GrpcStatus.UNAVAILABLE,
                        message="inventory offline",
                    ),
                },
            )
        ],
    )
    path = tmp_path / "grpc-plan.json"
    path.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _call(grpc_endpoint: str, proto: Path):
    index = GrpcDescriptorIndex.load(proto)
    method = index.method(GRPC_PATH)
    input_type = index.pool.FindMessageTypeByName(method.request_type)
    output_type = index.pool.FindMessageTypeByName(method.response_type)
    from google.protobuf import message_factory

    request_message = message_factory.GetMessageClass(input_type)
    response_message = message_factory.GetMessageClass(output_type)
    channel = grpc.insecure_channel(grpc_endpoint)
    grpc.channel_ready_future(channel).result(timeout=10)
    call = channel.unary_unary(
        GRPC_PATH,
        request_serializer=request_message.SerializeToString,
        response_deserializer=response_message.FromString,
    )
    return channel, call, request_message


def test_real_gripmock_success_error_history_and_cleanup(tmp_path: Path) -> None:
    plan_path = _plan(tmp_path)
    runtime = start_mock_plan(
        plan_path,
        history_path=tmp_path / "mock_calls.jsonl",
        gripmock_path=GRIPMOCK_BIN,
        grpc_start_timeout_s=20,
    )
    proto = tmp_path / "inventory.proto"
    channel, call, request_message = _call(runtime.grpc_endpoint, proto)
    headers = {"Authorization": f"Bearer {runtime.token}"}
    try:
        created = httpx.post(
            f"{runtime.server.endpoint}/_admin/sessions",
            headers=headers,
            json={
                "session_id": "S-success",
                "namespace": "gripmock-e2e",
                "case_id": "TC-success",
                "bindings": [{"dep_id": DEP_ID, "branch": "success"}],
            },
            timeout=5,
        )
        assert created.status_code == 200, created.text

        request = request_message()
        json_format.ParseDict({"sku": "SKU-1"}, request)
        response = call(request)
        assert response.available is True
        assert response.reservation_id == "R-1"

        drained = httpx.post(
            f"{runtime.server.endpoint}/_admin/sessions/S-success/drain",
            headers=headers,
            json={"timeout_ms": 500},
            timeout=5,
        )
        assert drained.status_code == 200, drained.text
        deleted = httpx.delete(
            f"{runtime.server.endpoint}/_admin/sessions/S-success",
            headers=headers,
            timeout=5,
        )
        assert deleted.status_code == 200, deleted.text

        created = httpx.post(
            f"{runtime.server.endpoint}/_admin/sessions",
            headers=headers,
            json={
                "session_id": "S-error",
                "namespace": "gripmock-e2e",
                "case_id": "TC-error",
                "bindings": [{"dep_id": DEP_ID, "branch": "unavailable"}],
            },
            timeout=5,
        )
        assert created.status_code == 200, created.text
        with pytest.raises(grpc.RpcError) as failure:
            request = request_message()
            json_format.ParseDict({"sku": "SKU-2"}, request)
            call(request)
        assert failure.value.code() is grpc.StatusCode.UNAVAILABLE
        assert "inventory offline" in failure.value.details()
        httpx.post(
            f"{runtime.server.endpoint}/_admin/sessions/S-error/drain",
            headers=headers,
            json={"timeout_ms": 500},
            timeout=5,
        )
        httpx.delete(
            f"{runtime.server.endpoint}/_admin/sessions/S-error",
            headers=headers,
            timeout=5,
        )
    finally:
        channel.close()
        runtime.stop()

    records = [
        json.loads(line)
        for line in runtime.server.evidence.path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert [item["branch"] for item in records] == ["success", "unavailable"]
    assert [item["grpc_status"] for item in records] == ["OK", "UNAVAILABLE"]
