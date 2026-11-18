"""统一 Mock Plan dep 视图与语义 transport 测试。"""
from __future__ import annotations

from astsight.mock import validate_mock_plan
from astsight.mock.semantic import build_mock_semantic, validate_mock_semantic
from contracts import (
    DubboMockLocator,
    DubboMockReply,
    HttpMockLocator,
    MockPlan,
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
