from __future__ import annotations

import pytest

from astsight.mock import (
    CallSite,
    DepIdError,
    assign_route_keys,
    build_dep_id,
    build_dep_id_plan,
    normalize_host,
    normalize_path_key,
    normalize_target_url,
)


def test_path_key_normalizes_placeholders_and_edges() -> None:
    assert normalize_path_key("/orders/{id}") == "/orders/{}"
    assert normalize_path_key("/orders/{id:\\d+}") == "/orders/{}"
    assert normalize_path_key("/orders/:orderId/") == "/orders/{}"
    assert normalize_path_key("/orders/{orderId}/items/{itemId}") == "/orders/{}/items/{}"
    assert normalize_path_key("//a//b/?x=1#frag") == "/a/b"
    assert normalize_path_key("/") == "/"
    assert normalize_path_key("") == "/"


def test_normalize_target_url_strips_default_port_and_query() -> None:
    target = normalize_target_url("http://LOCALHOST:80/orders/{id}?x=1")
    assert target.url == "http://localhost/orders/{}"
    assert target.host == "localhost"
    assert target.path_key == "/orders/{}"

    private = normalize_target_url("http://127.0.0.1:9001/health")
    assert private.host == "127.0.0.1:9001"
    assert private.url == "http://127.0.0.1:9001/health"


def test_normalize_host_keeps_private_non_default_port() -> None:
    assert normalize_host("127.0.0.1", 9001) == "127.0.0.1:9001"
    assert normalize_host("127.0.0.1", 80) == "127.0.0.1"
    assert normalize_host("LOCALHOST", 8080) == "localhost:8080"
    assert normalize_host("::1", 9001) == "[::1]:9001"


def test_same_endpoint_multiple_call_sites_merge() -> None:
    plan = build_dep_id_plan(
        [
            CallSite(
                "a",
                "post",
                "http://pay-service/gateway/pay/",
                service_name="Pay-Service",
            ),
            CallSite(
                "b",
                "POST",
                "http://pay-service:80/gateway/pay",
                service_name="pay-service",
            ),
        ]
    )
    assert len(plan.deps) == 1
    dep = plan.deps[0]
    assert dep.dep_id == "http:svc:pay-service POST /gateway/pay"
    assert dep.call_sites == ["a", "b"]
    assert dep.route_host == "pay-service"
    assert dep.inferred is False
    assert plan.unsupported == []


def test_svc_beats_cfg_and_host() -> None:
    plan = build_dep_id_plan(
        [
            CallSite(
                "a",
                "GET",
                "http://10.0.0.9:8080/status",
                service_name="stock-service",
                config_key="stock.client.base-url",
            )
        ]
    )
    assert plan.deps[0].dep_id == "http:svc:stock-service GET /status"


def test_cfg_and_host_fallbacks() -> None:
    cfg = build_dep_id_plan(
        [CallSite("a", "POST", "http://pay.example/gateway/pay", config_key="pay.client.base-url")]
    )
    assert cfg.deps[0].dep_id == "http:cfg:pay.client.base-url POST /gateway/pay"

    host = build_dep_id_plan([CallSite("b", "GET", "http://127.0.0.1:9001/health")])
    assert host.deps[0].dep_id == "http:host:127.0.0.1:9001 GET /health"


def test_cross_service_same_path_does_not_collide() -> None:
    plan = build_dep_id_plan(
        [
            CallSite("a", "GET", "http://pay-service/api/v1/status", service_name="pay-service"),
            CallSite(
                "b", "GET", "http://stock-service/api/v1/status", service_name="stock-service"
            ),
        ]
    )
    assert {dep.dep_id for dep in plan.deps} == {
        "http:svc:pay-service GET /api/v1/status",
        "http:svc:stock-service GET /api/v1/status",
    }


def test_same_service_multiple_hosts_merges() -> None:
    plan = build_dep_id_plan(
        [
            CallSite("a", "GET", "http://10.0.0.1:8080/x", service_name="svc"),
            CallSite("b", "GET", "http://10.0.0.2:8080/x", service_name="svc"),
        ]
    )
    assert len(plan.deps) == 1
    assert plan.deps[0].targets == ["http://10.0.0.1:8080/x", "http://10.0.0.2:8080/x"]


def test_collision_is_resolved_with_target_hash() -> None:
    plan = build_dep_id_plan(
        [
            CallSite("a", "GET", "http://a.example/x", config_key="shared"),
            CallSite("b", "GET", "http://b.example/x", config_key="shared"),
        ]
    )
    assert len(plan.deps) == 2
    assert all("~" in dep.dep_id for dep in plan.deps)
    assert len(plan.collisions) == 2
    assert all(item.resolved for item in plan.collisions)


def test_unresolved_target_stays_call_scope_and_https_is_unsupported() -> None:
    plan = build_dep_id_plan(
        [
            CallSite("a", "GET", None, path="/orders/{id}", service_name="should-not-be-used"),
            CallSite("b", "GET", "https://secure.example/x"),
        ]
    )
    assert plan.deps[0].dep_id == "http:call:a GET /orders/{}"
    assert plan.deps[0].inferred is True
    assert plan.unsupported[0].reason == "tls_not_supported"


def test_route_keys_are_unique_and_stable() -> None:
    plan = build_dep_id_plan(
        [
            CallSite("a", "GET", "http://a/x", service_name="a"),
            CallSite("b", "GET", "http://b/x", service_name="b"),
        ]
    )
    keys = assign_route_keys(plan.deps)
    assert len(keys) == 2
    assert len(set(keys.values())) == 2
    assert all(key.startswith("r") and len(key) == 13 for key in keys.values())
    assert keys == assign_route_keys(plan.deps)


def test_build_dep_id_rejects_empty_method() -> None:
    with pytest.raises(DepIdError):
        build_dep_id("svc:x", "", "/x")
