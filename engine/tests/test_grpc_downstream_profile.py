"""gRPC downstream 自动画像测试。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert dep["config_key"] == "inventory.grpc.addr"
    assert dep["config_kind"] == "spring_property"


@pytest.mark.parametrize(
    ("expression", "property_name", "config_key", "config_kind"),
    [
        (
            'System.getenv("INVENTORY_ENV_ADDR")',
            "INVENTORY_ENV_ADDR",
            None,
            "env",
        ),
        (
            'System.getProperty("inventory.grpc.system.addr")',
            "INVENTORY_GRPC_SYSTEM_ADDR",
            "inventory.grpc.system.addr",
            "system_property",
        ),
    ],
)
def test_java_system_env_and_system_property_targets_are_profiled(
    tmp_path: Path,
    expression: str,
    property_name: str,
    config_key: str | None,
    config_kind: str,
) -> None:
    _proto(tmp_path)
    source = tmp_path / "src" / "OrderClient.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
public class OrderClient {
  private final String target = %s;
  InventoryServiceGrpc.InventoryServiceBlockingStub stub;
  void run() {
    stub.reserve(null);
  }
}
"""
        % expression,
        encoding="utf-8",
    )

    result = GrpcDownstreamScanner(tmp_path).scan()

    dep = result.deps[0]
    assert dep["property"] == property_name
    assert dep["config_key"] == config_key
    assert dep["config_kind"] == config_kind


def test_hardcoded_grpc_target_blocks_mock_injection(tmp_path: Path) -> None:
    _proto(tmp_path)
    (tmp_path / "app.py").write_text(
        """
import grpc
from inventory_pb2_grpc import InventoryServiceStub

channel = grpc.insecure_channel("127.0.0.1:50051")
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
        cases=[SimpleNamespace(mock_bindings={DEP_ID: "success"})]
    )

    plan, warnings = build_mock_plan(
        context,
        semantic,
        draft,
        sut_id="sut",
        project_root=tmp_path,
    )

    assert plan is not None
    assert plan.injections[0].strategy.value == "unsupported"
    assert plan.injections[0].reason == "grpc_target_hardcoded"
    assert f"mock_injection_unsupported:{DEP_ID}:grpc_target_hardcoded" in warnings


def test_java_configuration_properties_prefix_and_field_are_profiled(
    tmp_path: Path,
) -> None:
    _proto(tmp_path)
    source = tmp_path / "src" / "OrderClient.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
@ConfigurationProperties(prefix = "inventory.grpc")
class InventoryProperties {
  private String addr;
  String getAddr() { return addr; }
}

class OrderClient {
  private final InventoryProperties properties;
  InventoryServiceGrpc.InventoryServiceBlockingStub stub;
  void run() {
    String target = properties.getAddr();
    stub.reserve(null);
  }
}
""",
        encoding="utf-8",
    )

    dep = GrpcDownstreamScanner(tmp_path).scan().deps[0]
    assert dep["property"] == "INVENTORY_GRPC_ADDR"
    assert dep["config_key"] == "inventory.grpc.addr"
    assert dep["config_kind"] == "configuration_properties"


def test_python_settings_object_target_is_profiled(tmp_path: Path) -> None:
    _proto(tmp_path)
    (tmp_path / "app.py").write_text(
        """
import grpc
from settings import settings
from inventory_pb2_grpc import InventoryServiceStub

channel = grpc.insecure_channel(settings.inventory_grpc_addr)
stub = InventoryServiceStub(channel)

def handler():
    return stub.Reserve({})
""",
        encoding="utf-8",
    )

    dep = GrpcDownstreamScanner(tmp_path).scan().deps[0]
    assert dep["property"] == "INVENTORY_GRPC_ADDR"
    assert dep["config_kind"] == "env"
    assert dep["target"] is None


def test_node_generated_client_and_config_alias_are_profiled(
    tmp_path: Path,
) -> None:
    _proto(tmp_path)
    (tmp_path / "config.js").write_text(
        """
export const config = {
  inventoryGrpcAddr: process.env.NODE_INVENTORY_GRPC_ADDR,
};
""",
        encoding="utf-8",
    )
    (tmp_path / "client.ts").write_text(
        """
const client = new InventoryServiceClient(config.inventoryGrpcAddr, creds);
export function reserve() {
  return client.reserve({ sku: "S" });
}
""",
        encoding="utf-8",
    )

    result = GrpcDownstreamScanner(tmp_path).scan()
    dep = next(item for item in result.deps if item["languages"] == ["node"])
    assert dep["property"] == "NODE_INVENTORY_GRPC_ADDR"
    assert dep["config_kind"] == "env"


def test_go_generated_client_and_viper_target_are_profiled(
    tmp_path: Path,
) -> None:
    _proto(tmp_path)
    (tmp_path / "client.go").write_text(
        """
package main

import "google.golang.org/grpc"

func reserve() {
    conn, err := grpc.NewClient(
        viper.GetString("inventory.grpc.addr"),
        grpc.WithTransportCredentials(insecure.NewCredentials()),
    )
    client := NewInventoryServiceClient(conn)
    client.Reserve(context.Background(), &ReserveRequest{Sku: "S"})
}
""",
        encoding="utf-8",
    )

    result = GrpcDownstreamScanner(tmp_path).scan()
    dep = next(item for item in result.deps if item["languages"] == ["go"])
    assert dep["property"] == "INVENTORY_GRPC_ADDR"
    assert dep["config_kind"] == "env"


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
