"""Dubbo 消费端画像扫描与 MockPlan 生成。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astsight.agent.mockplan import build_mock_plan
from astsight.mock import MockPlanError, validate_mock_plan
from astsight.profile.builder import build_profiles
from astsight.profile.dubbo import scan_dubbo_sources
from contracts import (
    MockBranch,
    MockDep,
    MockPlan,
    MockPlanDubboRoute,
    MockSemantic,
    MockStub,
    Transport,
)

INTERFACE = """package com.acme;

import java.util.concurrent.CompletableFuture;

public interface OrderService {
    Order create(String id);

    Order create(String id, int amount);

    CompletableFuture<Order> createAsync(String id);
}
"""

CLIENT = """package com.acme;

import org.apache.dubbo.config.annotation.DubboReference;

public class OrderClient {
    @DubboReference(group = "order", version = "1.0.0")
    private OrderService orderService;

    public Order byId(String id) {
        return orderService.create(id);
    }

    public Order withAmount(String id) {
        return orderService.create(id, 3);
    }
}
"""

DEP_SINGLE = (
    "dubbo:com.acme.OrderService#create(java.lang.String)"
    "?group=order&version=1.0.0"
)
DEP_OVERLOAD = (
    "dubbo:com.acme.OrderService#create(java.lang.String,int)"
    "?group=order&version=1.0.0"
)


def _project(tmp_path: Path) -> Path:
    package = tmp_path / "src" / "main" / "java" / "com" / "acme"
    package.mkdir(parents=True)
    (package / "OrderService.java").write_text(INTERFACE, encoding="utf-8")
    (package / "OrderClient.java").write_text(CLIENT, encoding="utf-8")
    return tmp_path


def _scan(root: Path):
    files = [(path, path.suffix.lower() == ".java" and "java" or "") for path in sorted(root.rglob("*")) if path.is_file()]
    return scan_dubbo_sources(
        files=files,
        root=root,
        read_text=lambda path: path.read_text(encoding="utf-8"),
    )


def test_scan_recognizes_overloads_and_group_version(tmp_path: Path) -> None:
    result = _scan(_project(tmp_path))
    assert [dep["dep_id"] for dep in result.deps] == [DEP_SINGLE, DEP_OVERLOAD]
    assert all(dep["dep_type"] == "dubbo" for dep in result.deps)
    assert all(dep["service"] == "com.acme.OrderService" for dep in result.deps)
    assert all(dep["clients"] == ["dubbo"] for dep in result.deps)
    assert result.unsupported == []


def test_scan_marks_interface_in_jar_unsupported(tmp_path: Path) -> None:
    package = tmp_path / "src" / "com" / "acme"
    package.mkdir(parents=True)
    (package / "Client.java").write_text(
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboReference;

class Client {
    @DubboReference
    private ExternalService external;

    void run() { external.call(); }
}
""",
        encoding="utf-8",
    )
    result = _scan(tmp_path)
    assert result.deps == []
    assert result.unsupported[0]["reason"] == "dubbo_interface_in_jar"


def test_scan_marks_xml_reference_unsupported(tmp_path: Path) -> None:
    (tmp_path / "dubbo-consumer.xml").write_text(
        '<dubbo:reference id="orderService" interface="com.acme.OrderService"/>',
        encoding="utf-8",
    )
    result = _scan(tmp_path)
    assert result.deps == []
    assert result.unsupported[0]["reason"] == "dubbo_xml_reference_unsupported"


def test_build_profiles_attaches_dubbo_deps(tmp_path: Path) -> None:
    root = _project(tmp_path)
    profiles, _unmapped = build_profiles(
        [{"method": "POST", "path": "/orders", "handler": "createOrder"}],
        [],
        project_root=root,
    )
    dep_ids = {
        dep["dep_id"]
        for dep in profiles[0]["downstream_deps"]
        if dep["dep_id"].startswith("dubbo:")
    }
    assert dep_ids == {DEP_SINGLE, DEP_OVERLOAD}


def test_build_mock_plan_emits_dubbo_route_and_agent_injection(tmp_path: Path) -> None:
    context = {
        "profiles": [
            {
                "downstream_deps": [
                    {
                        "dep_id": DEP_SINGLE,
                        "dep_type": "dubbo",
                        "service": "com.acme.OrderService",
                        "method": "create",
                        "parameter_types": ["java.lang.String"],
                        "group": "order",
                        "version": "1.0.0",
                        "clients": ["dubbo"],
                        "languages": ["java"],
                    }
                ]
            }
        ]
    }
    semantic = MockSemantic(
        deps=[
            MockDep(
                dep_id=DEP_SINGLE,
                transport=Transport.DUBBO,
                branches=[
                    MockBranch(
                        branch="success",
                        semantics="创建成功",
                        stub=MockStub(body={"orderId": "O-1"}),
                    )
                ],
            )
        ]
    )
    draft = SimpleNamespace(
        cases=[SimpleNamespace(mock_bindings={DEP_SINGLE: "success"})]
    )
    plan, warnings = build_mock_plan(context, semantic, draft, sut_id="sut")
    assert plan is not None
    assert plan.routes == []
    assert len(plan.dubbo_routes) == 1
    route = plan.dubbo_routes[0]
    assert route.dep_id == DEP_SINGLE
    assert route.parameter_types == ["java.lang.String"]
    assert plan.injections[0].strategy.value == "agent_rewrite"
    assert plan.injections[0].reason == "dubbo_filter_requires_java_agent"
    assert any("mock_injection_dubbo_filter" in item for item in warnings)


def test_plan_rejects_dubbo_dep_id_without_signature() -> None:
    plan = MockPlan(
        schema_version=1,
        sut_id="sut",
        dubbo_routes=[
            MockPlanDubboRoute(
                dep_id="dubbo:com.acme.OrderService#create",
                service="com.acme.OrderService",
                method="create",
                parameter_types=[],
                branches={"success": MockStub(body={"ok": True})},
            )
        ],
    )
    with pytest.raises(MockPlanError) as exc:
        validate_mock_plan(plan)
    assert exc.value.code == "mock_dep_id_invalid"
