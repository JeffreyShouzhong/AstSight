"""统一 Mock Plan dep 视图与语义 transport 测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astsight.mock import (
    MockPlanError,
    load_mock_plan,
    start_mock_plan,
    validate_mock_plan,
)
from astsight.mock.semantic import build_mock_semantic, validate_mock_semantic
from contracts import (
    DubboMockLocator,
    DubboMockReply,
    GrpcMockLocator,
    GrpcMockReply,
    HttpMockLocator,
    MockPlan,
    MockPlanDep,
    MockPlanDubboRoute,
    MockPlanInjection,
    MockPlanRoute,
    MockStrategy,
    MockStub,
    Transport,
)

HTTP_DEP = "http:svc:user GET /users/{}"
DUBBO_DEP = (
    "dubbo:com.acme.OrderService#create(java.lang.String)"
    "?group=order&version=1.0.0"
)


def test_validated_plan_projects_http_and_dubbo_to_unified_deps() -> None:
    plan = MockPlan(
        sut_id="sut",
        routes=[
            MockPlanRoute(
                dep_id=HTTP_DEP,
                method="GET",
                path_key="/users/{}",
                host="user-service",
                target="http://user-service/users/1",
                branches={"success": MockStub(body={"id": "U-1"})},
            )
        ],
        dubbo_routes=[
            MockPlanDubboRoute(
                dep_id=DUBBO_DEP,
                service="com.acme.OrderService",
                method="create",
                parameter_types=["java.lang.String"],
                group="order",
                version="1.0.0",
                branches={"success": MockStub(body={"orderId": "O-1"})},
            )
        ],
        injections=[
            MockPlanInjection(
                dep_id=HTTP_DEP,
                strategy=MockStrategy.URL_OVERRIDE,
                kind="base_url",
                property="USER_BASE_URL",
                original="http://user-service",
            ),
            MockPlanInjection(
                dep_id=DUBBO_DEP,
                strategy=MockStrategy.AGENT_REWRITE,
                kind="base_url",
                reason="dubbo_filter_requires_java_agent",
            ),
        ],
    )

    validate_mock_plan(plan)

    assert [dep.transport for dep in plan.deps] == [
        Transport.HTTP,
        Transport.DUBBO,
    ]
    assert isinstance(plan.deps[0].locator, HttpMockLocator)
    assert isinstance(plan.deps[1].locator, DubboMockLocator)
    assert plan.deps[1].strategy is MockStrategy.AGENT_REWRITE


def test_mock_semantic_uses_dubbo_transport_and_reply() -> None:
    context = {
        "profiles": [
            {
                "endpoint": "dubbo:com.acme.OrderService#create",
                "downstream_deps": [
                    {
                        "dep_id": DUBBO_DEP,
                        "dep_type": "dubbo",
                        "service": "com.acme.OrderService",
                        "method": "create",
                        "parameter_types": ["java.lang.String"],
                        "group": "order",
                        "version": "1.0.0",
                    }
                ],
            }
        ]
    }

    semantic = build_mock_semantic(context)
    validate_mock_semantic(semantic, context)

    dep = semantic.deps[0]
    assert dep.transport is Transport.DUBBO
    assert dep.strategy is MockStrategy.AGENT_REWRITE
    assert isinstance(dep.branches[0].reply, DubboMockReply)


def test_grpc_deps_only_plan_validates_and_blocks_missing_runtime(
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
    grpc_dep = MockPlanDep(
        dep_id="grpc:/inventory.v1.InventoryService/Reserve",
        transport=Transport.GRPC,
        strategy=MockStrategy.GRPC_SERVER,
        locator=GrpcMockLocator(
            grpc_path="/inventory.v1.InventoryService/Reserve",
            target="127.0.0.1:50051",
            descriptor_source="inventory.proto",
        ),
        branches={"success": MockStub(body={"available": True})},
        replies={"success": GrpcMockReply(body={"available": True})},
    )
    plan = MockPlan(sut_id="sut", deps=[grpc_dep])

    validate_mock_plan(plan)

    assert plan.routes == []
    assert plan.dubbo_routes == []
    assert plan.deps[0].route_key is not None

    plan_path = tmp_path / "grpc-plan.json"
    plan_path.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(MockPlanError) as error:
        start_mock_plan(plan_path)
    assert error.value.code == "mock_grpc_runtime_unimplemented"


def test_grpc_plan_rejects_invalid_response_body(tmp_path: Path) -> None:
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
                strategy=MockStrategy.GRPC_SERVER,
                locator=GrpcMockLocator(
                    grpc_path="/inventory.v1.InventoryService/Reserve",
                    descriptor_source="inventory.proto",
                ),
                branches={"success": MockStub(body={"unknown": True})},
                replies={"success": GrpcMockReply(body={"unknown": True})},
            )
        ],
    )
    plan_path = tmp_path / "grpc-plan-invalid.json"
    plan_path.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(MockPlanError) as error:
        load_mock_plan(plan_path)
    assert error.value.code == "mock_stub_invalid"
