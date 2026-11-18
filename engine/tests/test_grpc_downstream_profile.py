"""gRPC downstream 自动画像测试。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from astsight.mock import validate_mock_plan
from astsight.agent.mockplan import build_mock_plan
from astsight.mock.semantic import build_mock_semantic
from astsight.profile import (
    GrpcDownstreamScanner,
    build_plan_from_profiles,
    build_profiles,
)
from contracts import MockPlan

PROTO = """
syntax = "proto3";
package inventory.v1;
message ReserveRequest { string sku = 1; }
message ReserveResponse { bool available = 1; }
service InventoryService {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
"""
DEP_ID = "grpc:/inventory.v1.InventoryService/Reserve"


def _proto(root: Path) -> None:
    (root / "inventory.proto").write_text(PROTO, encoding="utf-8")


def _route() -> list[dict]:
    return [
        {
            "method": "GET",
            "path": "/orders",
            "handler": "orders.handler",
            "file": "app.py",
            "line": 1,
        }
    ]


def test_python_grpc_stub_call_is_profiled_and_projected_to_plan(
    tmp_path: Path,
) -> None:
    _proto(tmp_path)
    (tmp_path / "app.py").write_text(
        """
import os
import grpc
from inventory_pb2_grpc import InventoryServiceStub

channel = grpc.insecure_channel(os.environ["INVENTORY_GRPC_ADDR"])
stub = InventoryServiceStub(channel)

def handler():
    return stub.Reserve({})
""",
        encoding="utf-8",
    )

    profiles, _unmapped = build_profiles(
        _route(),
        [],
        project_root=tmp_path,
    )
    dep = next(
        item
        for item in profiles[0]["downstream_deps"]
        if item["dep_id"] == DEP_ID
    )

    assert dep["service"] == "inventory.v1.InventoryService"
    assert dep["method"] == "Reserve"
    assert dep["property"] == "INVENTORY_GRPC_ADDR"
    assert dep["descriptor_source"] == "inventory.proto"

    payload = build_plan_from_profiles(profiles, sut_id="sut")
    plan = MockPlan.model_validate(payload)
    validate_mock_plan(plan)
    assert plan.deps[0].dep_id == DEP_ID
    assert plan.deps[0].locator.grpc_path == "/inventory.v1.InventoryService/Reserve"


def test_java_grpc_stub_and_value_property_are_profiled(tmp_path: Path) -> None:
    _proto(tmp_path)
    source = tmp_path / "src" / "OrderClient.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
public class OrderClient {
  @Value("${inventory.grpc.addr}") private String address;
  InventoryServiceGrpc.InventoryServiceBlockingStub stub;
  void run() {
    stub.reserve(null);
  }
}
""",
        encoding="utf-8",
    )

    result = GrpcDownstreamScanner(tmp_path).scan()

    assert len(result.deps) == 1
    dep = result.deps[0]
    assert dep["dep_id"] == DEP_ID
    assert dep["property"] == "INVENTORY_GRPC_ADDR"
    assert dep["languages"] == ["java"]


def test_agent_mock_plan_contains_grpc_reply_and_injection(tmp_path: Path) -> None:
    _proto(tmp_path)
    (tmp_path / "app.py").write_text(
        """
import os
import grpc
from inventory_pb2_grpc import InventoryServiceStub

channel = grpc.insecure_channel(os.environ["INVENTORY_GRPC_ADDR"])
stub = InventoryServiceStub(channel)

def handler():
    return stub.Reserve({})
""",
        encoding="utf-8",
    )
    profiles, _unmapped = build_profiles(
        _route(),
        [],
        project_root=tmp_path,
    )
    context = {"profiles": profiles}
    semantic = build_mock_semantic(context)
    draft = SimpleNamespace(
        cases=[
            SimpleNamespace(
                mock_bindings={DEP_ID: "success"},
            )
        ]
    )

    plan, warnings = build_mock_plan(
        context,
        semantic,
        draft,
        sut_id="sut",
        project_root=tmp_path,
    )

    assert plan is not None
    assert plan.deps[0].dep_id == DEP_ID
    assert plan.deps[0].locator.descriptor_source == str(
        (tmp_path / "inventory.proto").resolve()
    )
    assert plan.deps[0].replies["success"].body == {
        "available": False,
    }
    assert plan.injections[0].strategy.value == "grpc_server"
    assert plan.injections[0].property == "INVENTORY_GRPC_ADDR"
    assert f"mock_injection_grpc_server:{DEP_ID}" in warnings
