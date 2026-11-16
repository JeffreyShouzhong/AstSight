"""Mock 路由解析优先级：Host 组 > 字面量路径 > 唯一 method+path > 歧义。"""
from __future__ import annotations

from astsight.mock import RouteTable
from contracts import MockRoute, MockRouteMatch


def _table(*routes: tuple[str, str, str, str | None]) -> RouteTable:
    """按 (route_key, dep_id, path_key, host) 建表。"""
    table = RouteTable()
    for index, (route_key, dep_id, path_key, host) in enumerate(routes):
        table.add_route(
            MockRoute(
                route_key=route_key,
                dep_id=dep_id,
                method="GET",
                path_key=path_key,
                host=host,
            )
        )
    return table


def test_exact_path_wins_over_template_by_host() -> None:
    table = _table(
        ("r000000000001", "http:host:api.example.com GET /orders/7", "/orders/7", "api.example.com"),
        ("r000000000002", "http:host:api.example.com GET /orders/{}", "/orders/{}", "api.example.com"),
    )

    exact = table.resolve("GET", "/orders/7", host="api.example.com")
    assert exact.error is None
    assert exact.entry is not None
    assert exact.entry.route.route_key == "r000000000001"
    assert exact.route_match == MockRouteMatch.HOST_PATH

    # 模板路径只被模板命中，不受字面量影响。
    template = table.resolve("GET", "/orders/8", host="api.example.com")
    assert template.entry is not None
    assert template.entry.route.route_key == "r000000000002"


def test_exact_path_wins_when_host_is_unknown() -> None:
    table = _table(
        ("r000000000001", "http:svc:a GET /orders/7", "/orders/7", "a"),
        ("r000000000002", "http:svc:b GET /orders/{}", "/orders/{}", "b"),
    )

    resolution = table.resolve("GET", "/orders/7", host="unknown")

    assert resolution.error is None
    assert resolution.entry is not None
    assert resolution.entry.route.route_key == "r000000000001"
    assert resolution.route_match == MockRouteMatch.METHOD_PATH


def test_two_templates_stay_ambiguous() -> None:
    table = _table(
        ("r000000000001", "http:svc:a GET /orders/{}", "/orders/{}", "a"),
        ("r000000000002", "http:svc:b GET /orders/{}", "/orders/{}", "a"),
    )

    resolution = table.resolve("GET", "/orders/7", host="a")

    assert resolution.error == "mock_route_ambiguous"
    assert resolution.candidates == ["r000000000001", "r000000000002"]


def test_two_exact_paths_stay_ambiguous() -> None:
    table = _table(
        ("r000000000001", "http:svc:a GET /orders/7", "/orders/7", "a"),
        ("r000000000002", "http:svc:b GET /orders/7", "/orders/7", "a"),
        ("r000000000003", "http:svc:c GET /orders/{}", "/orders/{}", "a"),
    )

    resolution = table.resolve("GET", "/orders/7", host="a")

    assert resolution.error == "mock_route_ambiguous"
    # 无法唯一化时保留全部候选，便于定位冲突的规划。
    assert resolution.candidates == [
        "r000000000001",
        "r000000000002",
        "r000000000003",
    ]
