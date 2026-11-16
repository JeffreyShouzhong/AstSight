"""把 Mock 语义分支 + 用例绑定落成可启动的 MockPlan（确定性转换，不再调用 LLM）。

输入既可以是新的 `MockSemantic`，也可以兼容旧 `SemanticAnalysis`；两者都提供
`branch_index()`，但事实源应逐步统一为 `MockSemantic`。
"""
from __future__ import annotations

from typing import Any

from contracts import (
    MockPlan,
    MockPlanDubboRoute,
    MockPlanInjection,
    MockPlanRoute,
    MockSemantic,
    MockStrategy,
)

from ..mock import MockPlanError, validate_mock_plan
from ..profile.downstream import AGENT_REWRITE_CLIENTS
from .errors import AgentError
from .models import SemanticAnalysis


def _dep_index(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for profile in context.get("profiles") or []:
        for dep in profile.get("downstream_deps") or []:
            dep_id = str(dep.get("dep_id") or "")
            if dep_id and dep_id not in index:
                index[dep_id] = dep
    return index


_JAVA_CLIENTS = {
    "okhttp",
    "feign",
    "resttemplate",
    "webclient",
    "httpclient",
    "apache-httpclient",
}
_JAVA_SUPPORTED_CLIENTS = set(AGENT_REWRITE_CLIENTS)


def _is_java_dep(dep: dict[str, Any]) -> bool:
    languages = {str(item) for item in dep.get("languages") or []}
    if "java" in languages or str(dep.get("language") or "") == "java":
        return True
    return bool({str(item) for item in dep.get("clients") or []} & _JAVA_CLIENTS)


def _java_injection(dep: dict[str, Any], dep_id: str) -> MockPlanInjection:
    """Java 只支持 OkHttp/Feign/RestTemplate/直连 Apache HttpClient。

    WebClient/JDK HttpClient 不为它们建设专用 adapter；使用自有传输或
    shaded HTTP 客户端的第三方 SDK 也不建。若地址来自可覆盖配置，仍允许
    `url_override`（纯配置注入，不依赖 agent）。
    """
    config_keys = [str(key) for key in dep.get("config_keys") or [] if str(key)]
    target = str(dep.get("target") or "")
    https = target.lower().startswith("https://")
    clients = {str(item) for item in dep.get("clients") or []}
    unsupported_clients = clients - _JAVA_SUPPORTED_CLIENTS
    unsupported = bool(unsupported_clients)
    unknown = not clients

    if https:
        # HTTPS 只在 agent_rewrite 下放开；配置注入不改 scheme。
        if unsupported:
            return _unsupported(dep_id, "https_requires_supported_agent_client")
        if unknown:
            return _unsupported(dep_id, "client_unknown")
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.AGENT_REWRITE,
            kind="base_url",
            property=None,
            original=None,
            path_mode="append",
            reason="https_requires_agent_rewrite",
        )
    if config_keys:
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.URL_OVERRIDE,
            kind="base_url",
            property=config_keys[0],
            original=None,
            path_mode="append",
            reason="original_unknown",
        )
    if dep.get("evidence") == "svc" and dep.get("service_name"):
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.DISCOVERY_OVERRIDE,
            service_name=str(dep["service_name"]),
            kind="base_url",
            property=None,
            original=None,
            path_mode="append",
            reason="discovery_requires_static_instances",
        )
    if unsupported:
        return _unsupported(dep_id, "client_unsupported")
    if unknown:
        return _unsupported(dep_id, "client_unknown")
    if dep.get("target"):
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.AGENT_REWRITE,
            kind="base_url",
            property=None,
            original=None,
            path_mode="append",
            reason="agent_rewrite_required",
        )
    return _unsupported(dep_id, "target_unresolved")


def _unsupported(dep_id: str, reason: str) -> MockPlanInjection:
    return MockPlanInjection(
        dep_id=dep_id,
        strategy=MockStrategy.UNSUPPORTED,
        kind="base_url",
        property=None,
        original=None,
        path_mode="append",
        reason=reason,
    )


def _dubbo_route(
    dep: dict[str, Any], dep_id: str, branches: dict[str, Any]
) -> MockPlanDubboRoute:
    service = str(dep.get("service") or "").strip()
    method = str(dep.get("method") or "").strip()
    if not service or not method:
        raise AgentError(
            "mock_route_incomplete",
            f"{dep_id} 缺少 service/method，不能生成 Dubbo 路由",
        )
    parameter_types = [
        str(item) for item in dep.get("parameter_types") or [] if str(item)
    ]
    return MockPlanDubboRoute(
        dep_id=dep_id,
        service=service,
        method=method,
        parameter_types=parameter_types,
        group=str(dep.get("group") or "") or None,
        version=str(dep.get("version") or "") or None,
        source="agent",
        branches=branches,
    )


def _injection_for(dep: dict[str, Any]) -> MockPlanInjection:
    dep_id = str(dep.get("dep_id") or "")
    if dep_id.startswith("dubbo:") or dep.get("dep_type") == "dubbo":
        # 只走 Java agent 的消费端 Filter；没有其它注入方式。
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.AGENT_REWRITE,
            kind="base_url",
            property=None,
            original=None,
            path_mode="append",
            reason="dubbo_filter_requires_java_agent",
        )
    if _is_java_dep(dep):
        return _java_injection(dep, dep_id)
    config_keys = [str(key) for key in dep.get("config_keys") or [] if str(key)]
    if config_keys:
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.URL_OVERRIDE,
            kind="base_url",
            property=config_keys[0],
            original=None,
            path_mode="append",
            reason="original_unknown",
        )
    if dep.get("evidence") == "svc" and dep.get("service_name"):
        # 服务发现型下游（如 Feign + @FeignClient(name=...)）没有配置键可改，
        # 只能把服务名指到 Mock 主机；环境不支持时由 injection_env 显式告警。
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.DISCOVERY_OVERRIDE,
            service_name=str(dep["service_name"]),
            kind="base_url",
            property=None,
            original=None,
            path_mode="append",
            reason="discovery_requires_static_instances",
        )
    if dep.get("target"):
        return MockPlanInjection(
            dep_id=dep_id,
            strategy=MockStrategy.HTTP_PROXY,
            kind="proxy",
            property=None,
            original=None,
            path_mode="append",
            reason="proxy_requires_sut_env",
        )
    return MockPlanInjection(
        dep_id=dep_id,
        strategy=MockStrategy.UNSUPPORTED,
        kind="base_url",
        property=None,
        original=None,
        path_mode="append",
        reason="target_unresolved",
    )


def sync_mock_semantic(
    runtime: Any, semantic: MockSemantic
) -> list[str]:
    """把修复后的 Mock 分支热更新到已启动的路由表。

    服务端在每次请求时按分支名取 stub，因此替换 `RouteEntry.branches`
    即可让下一轮执行看到新响应；不重启服务，避免丢掉端口与会话上下文。
    """
    changed: list[str] = []
    dep_index = semantic.dep_index()
    for entry in runtime.server.route_table.entries:
        dep = dep_index.get(entry.route.dep_id)
        if dep is None:
            continue
        for branch in dep.branches:
            current = entry.branches.get(branch.branch)
            if current == branch.stub:
                continue
            entry.branches[branch.branch] = branch.stub.model_copy(deep=True)
            changed.append(f"{entry.route.dep_id}/{branch.branch}")
    for entry in runtime.server.dubbo_deps.values():
        dep = dep_index.get(entry.dep_id)
        if dep is None:
            continue
        for branch in dep.branches:
            current = entry.branches.get(branch.branch)
            if current == branch.stub:
                continue
            entry.branches[branch.branch] = branch.stub.model_copy(deep=True)
            changed.append(f"{entry.dep_id}/{branch.branch}")
    for route in [*runtime.plan.routes, *runtime.plan.dubbo_routes]:
        dep = dep_index.get(route.dep_id)
        if dep is None:
            continue
        route.branches = {
            branch.branch: branch.stub.model_copy(deep=True)
            for branch in dep.branches
        }
    return changed


def build_mock_plan(
    context: dict[str, Any],
    semantic: MockSemantic | SemanticAnalysis,
    draft: Any,
    *,
    sut_id: str,
    namespace: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[MockPlan | None, list[str]]:
    """仅使用用例实际绑定的 dep_id/branch；未绑定即不生成路由。"""
    bindings: dict[str, set[str]] = {}
    for case in getattr(draft, "cases", []) or []:
        for dep_id, branch in (getattr(case, "mock_bindings", {}) or {}).items():
            bindings.setdefault(str(dep_id), set()).add(str(branch))
    if not bindings:
        return None, ["mock_plan_skipped:no_case_bindings"]

    deps = _dep_index(context)
    branches = semantic.branch_index()
    warnings: list[str] = []
    routes: list[MockPlanRoute] = []
    dubbo_routes: list[MockPlanDubboRoute] = []
    injections: list[MockPlanInjection] = []

    for dep_id in sorted(bindings):
        dep = deps.get(dep_id)
        if dep is None:
            raise AgentError("mock_dep_unknown", f"context 中不存在下游: {dep_id}")
        route_branches = {}
        for branch in sorted(bindings[dep_id]):
            spec = branches.get((dep_id, branch))
            if spec is None:
                raise AgentError(
                    "mock_branch_unknown",
                    f"Mock 语义中没有分支: {dep_id} / {branch}",
                )
            route_branches[branch] = spec.stub
        if dep_id.startswith("dubbo:") or dep.get("dep_type") == "dubbo":
            dubbo_routes.append(_dubbo_route(dep, dep_id, route_branches))
        else:
            method = str(dep.get("method") or "").upper()
            path_key = str(dep.get("path_key") or "")
            if not method or not path_key:
                raise AgentError(
                    "mock_route_incomplete",
                    f"{dep_id} 缺少 method/path_key，不能生成路由",
                )
            routes.append(
                MockPlanRoute(
                    dep_id=dep_id,
                    method=method,
                    path_key=path_key,
                    host=dep.get("route_host") or None,
                    target=dep.get("target") or None,
                    source="agent",
                    branches=route_branches,
                )
            )
        injection = _injection_for(dep)
        injections.append(injection)
        if injection.strategy is MockStrategy.UNSUPPORTED:
            warnings.append(
                f"mock_injection_unsupported:{dep_id}:"
                f"{injection.reason or 'unsupported'}"
            )
        elif injection.strategy is MockStrategy.AGENT_REWRITE:
            if dep_id.startswith("dubbo:"):
                warnings.append(f"mock_injection_dubbo_filter:{dep_id}")
            else:
                warnings.append(f"mock_injection_agent_rewrite:{dep_id}")
        elif injection.strategy is MockStrategy.HTTP_PROXY:
            warnings.append(f"mock_injection_proxy:{dep_id}")
        elif injection.strategy is MockStrategy.DISCOVERY_OVERRIDE:
            warnings.append(
                f"mock_injection_discovery:{dep_id}:"
                "requires_spring_cloud_static_instances"
            )

    plan = MockPlan(
        schema_version=1,
        sut_id=sut_id,
        namespace=namespace or sut_id,
        host=host,
        port=port,
        routes=routes,
        dubbo_routes=dubbo_routes,
        injections=injections,
    )
    try:
        validation = validate_mock_plan(plan)
    except MockPlanError as exc:
        raise AgentError("mock_plan_invalid", f"{exc.code}: {exc.detail}") from exc
    warnings.extend(validation.warnings)
    return plan, warnings


__all__ = ["build_mock_plan", "sync_mock_semantic"]
