"""Mock 运行计划的加载、校验与内嵌服务生命周期（C3.5.5 P0）。

计划由 C3 画像或人工编写；本模块把它变成可启动的 `MockHttpServer`，
并生成不含 token 的 `run_manifest.json` / `mock_injection.json` 形态。
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contracts import (
    MockInjection,
    MockPlan,
    MockPlanInjection,
    MockPlanRoute,
    MockRoute,
    MockStrategy,
)

from .identity import (
    DepIdEntry,
    TlsNotSupportedError,
    assign_route_keys,
    normalize_method,
    normalize_path_key,
    normalize_target_url,
)
from .server import MockHttpServer

MAX_PLAN_BYTES = 4 * 1024 * 1024
MAX_PLAN_ROUTES = 1_000
MAX_BRANCHES_PER_ROUTE = 64
MAX_PLAN_INJECTIONS = 1_000
MAX_DEP_ID_CHARS = 512
MAX_BRANCH_CHARS = 128
MAX_PROPERTY_CHARS = 256
MAX_SUT_ID_CHARS = 128
_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_INJECTION_KINDS = {"base_url", "endpoint_url", "proxy"}
_PATH_MODES = {"append", "replace"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class MockPlanError(ValueError):
    """计划不合法；调用方应阻断，不启动服务。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class MockPlanValidation:
    route_keys: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def resolve_plan_path(project: str | Path, raw: str | Path) -> Path:
    """解析计划路径：绝对路径原样；相对路径先 cwd、再 project。"""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    cwd_candidate = (Path.cwd() / candidate).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (Path(project).resolve() / candidate).resolve()


def load_mock_plan(path: str | Path) -> MockPlan:
    plan_path = Path(path)
    if not plan_path.is_file():
        raise MockPlanError("mock_plan_missing", str(plan_path))
    size = plan_path.stat().st_size
    if size > MAX_PLAN_BYTES:
        raise MockPlanError(
            "mock_plan_too_large", f"{size} bytes > {MAX_PLAN_BYTES} bytes"
        )
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise MockPlanError("mock_plan_invalid_json", str(exc)[:300]) from exc
    if not isinstance(payload, dict):
        raise MockPlanError("mock_plan_invalid", "顶层必须是 JSON object")
    try:
        plan = MockPlan.model_validate(payload)
    except ValueError as exc:
        raise MockPlanError("mock_plan_invalid", str(exc)[:800]) from exc
    validate_mock_plan(plan)
    return plan


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise MockPlanError(code, detail)


def _normalize_route(route: MockPlanRoute, index: int) -> None:
    where = f"routes[{index}]"
    dep_id = str(route.dep_id or "").strip()
    _require(bool(dep_id), "mock_dep_id_invalid", f"{where}: 空 dep_id")
    _require(
        dep_id.startswith("http:"),
        "mock_dep_id_invalid",
        f"{where}: dep_id 必须以 http: 开头",
    )
    _require(
        len(dep_id) <= MAX_DEP_ID_CHARS,
        "mock_dep_id_invalid",
        f"{where}: dep_id 超过 {MAX_DEP_ID_CHARS} 字符",
    )

    method = normalize_method(route.method)
    _require(
        method in _VALID_METHODS,
        "mock_method_invalid",
        f"{where}: {route.method!r}",
    )

    _require(
        bool(str(route.path_key or "").strip()),
        "mock_path_invalid",
        f"{where}: 空 path_key",
    )
    path_key = normalize_path_key(route.path_key)

    host = route.host
    if host is not None:
        host = str(host).strip().lower().rstrip(".")
        _require(
            bool(host) and "://" not in host and "/" not in host,
            "mock_host_invalid",
            f"{where}: {route.host!r}",
        )

    target = route.target
    if target:
        try:
            normalized = normalize_target_url(str(target))
        except TlsNotSupportedError as exc:
            raise MockPlanError("tls_not_supported", f"{where}: {exc.detail}") from exc
        except ValueError as exc:
            raise MockPlanError("mock_target_invalid", f"{where}: {exc}") from exc
        target = normalized.url
        if not host:
            host = normalized.host

    _require(
        len(route.branches) <= MAX_BRANCHES_PER_ROUTE,
        "mock_plan_too_many_branches",
        f"{where}: {len(route.branches)} > {MAX_BRANCHES_PER_ROUTE}",
    )
    for name in route.branches:
        _require(
            bool(str(name).strip()) and len(str(name)) <= MAX_BRANCH_CHARS,
            "mock_branch_invalid",
            f"{where}: 分支名 {name!r}",
        )

    route.dep_id = dep_id
    route.method = method
    route.path_key = path_key
    route.host = host
    route.target = target


def validate_mock_plan(plan: MockPlan) -> MockPlanValidation:
    """校验并就地规范化计划；返回 route_key 与告警。"""
    _require(plan.schema_version == 1, "mock_plan_schema_unsupported", str(plan.schema_version))
    sut_id = str(plan.sut_id or "").strip()
    _require(bool(sut_id), "mock_sut_id_invalid", "sut_id 为空")
    _require(
        len(sut_id) <= MAX_SUT_ID_CHARS and not any(ch.isspace() for ch in sut_id),
        "mock_sut_id_invalid",
        repr(plan.sut_id),
    )
    namespace = str(plan.namespace or sut_id).strip()
    _require(
        bool(namespace)
        and len(namespace) <= MAX_SUT_ID_CHARS
        and not any(ch.isspace() for ch in namespace),
        "mock_namespace_invalid",
        repr(plan.namespace),
    )
    plan.sut_id = sut_id
    plan.namespace = namespace
    host = str(plan.host or "").strip()
    _require(bool(host), "mock_host_invalid", "监听 host 为空")
    _require("://" not in host and "/" not in host, "mock_host_invalid", repr(plan.host))
    if ":" in host:
        # 监听地址必须是裸 host；带端口或非 IPv6 的冒号写法会让 endpoint 失效。
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError as exc:
            raise MockPlanError("mock_host_invalid", repr(plan.host)) from exc
    plan.host = host

    _require(bool(plan.routes), "mock_plan_empty", "至少需要一条 route")
    _require(
        len(plan.routes) <= MAX_PLAN_ROUTES,
        "mock_plan_too_many_routes",
        f"{len(plan.routes)} > {MAX_PLAN_ROUTES}",
    )

    validation = MockPlanValidation()
    if host.lower() not in LOOPBACK_HOSTS:
        validation.warnings.append(f"mock_bind_non_loopback:{host}")
    seen_deps: set[str] = set()
    deps: list[DepIdEntry] = []
    for index, route in enumerate(plan.routes):
        _normalize_route(route, index)
        _require(
            route.dep_id not in seen_deps,
            "mock_route_conflict",
            f"重复 dep_id: {route.dep_id}",
        )
        seen_deps.add(route.dep_id)
        if not route.branches:
            validation.warnings.append(f"mock_branch_empty:{route.dep_id}")
        deps.append(
            DepIdEntry(
                dep_id=route.dep_id,
                downstream_key="",
                evidence="call",
                method=route.method,
                path_key=route.path_key,
                target=route.target,
            )
        )

    try:
        route_keys = assign_route_keys(deps)
    except ValueError as exc:
        raise MockPlanError("mock_route_conflict", str(exc)) from exc
    seen_keys: set[str] = set()
    for route in plan.routes:
        computed = route_keys[route.dep_id]
        if route.route_key is not None and route.route_key != computed:
            raise MockPlanError(
                "mock_route_key_mismatch",
                f"{route.dep_id}: {route.route_key!r} != {computed!r}",
            )
        _require(
            computed not in seen_keys,
            "mock_route_conflict",
            f"重复 route_key: {computed}",
        )
        seen_keys.add(computed)
        route.route_key = computed
    validation.route_keys = route_keys

    _require(
        len(plan.injections) <= MAX_PLAN_INJECTIONS,
        "mock_plan_too_many_injections",
        f"{len(plan.injections)} > {MAX_PLAN_INJECTIONS}",
    )
    inject_by_dep: dict[str, MockPlanInjection] = {}
    for index, injection in enumerate(plan.injections):
        where = f"injections[{index}]"
        dep_id = str(injection.dep_id or "").strip()
        _require(
            dep_id in seen_deps,
            "mock_injection_unknown_dep",
            f"{where}: {dep_id!r}",
        )
        _require(
            dep_id not in inject_by_dep,
            "mock_injection_duplicate",
            f"{dep_id}",
        )
        _require(
            injection.kind in _INJECTION_KINDS,
            "mock_injection_invalid",
            f"{where}: kind={injection.kind!r}",
        )
        _require(
            injection.path_mode in _PATH_MODES,
            "mock_injection_invalid",
            f"{where}: path_mode={injection.path_mode!r}",
        )
        if injection.property is not None:
            _require(
                len(injection.property) <= MAX_PROPERTY_CHARS,
                "mock_injection_invalid",
                f"{where}: property 过长",
            )
        if injection.strategy is MockStrategy.URL_OVERRIDE:
            _require(
                bool(injection.property),
                "mock_config_not_overridable",
                f"{where}: url_override 必须提供 property",
            )
            if not injection.original:
                # 环境变量/配置键形式的基址在分析阶段可能拿不到原值；
                # 允许显式声明 original_unknown，但必须留下告警，不能静默。
                _require(
                    injection.reason == "original_unknown",
                    "mock_config_not_overridable",
                    f"{where}: url_override 必须提供 original"
                    "（或 reason=original_unknown）",
                )
                validation.warnings.append(
                    f"mock_injection_original_unknown:{dep_id}"
                )
        if injection.strategy is MockStrategy.UNSUPPORTED:
            _require(
                bool(injection.reason),
                "mock_injection_invalid",
                f"{where}: unsupported 必须提供 reason",
            )
        injection.dep_id = dep_id
        inject_by_dep[dep_id] = injection

    for route in plan.routes:
        injection = inject_by_dep.get(route.dep_id)
        if injection is not None:
            if route.strategy is not None and route.strategy is not injection.strategy:
                raise MockPlanError(
                    "mock_strategy_mismatch",
                    f"{route.dep_id}: route={route.strategy.value} "
                    f"injection={injection.strategy.value}",
                )
            route.strategy = injection.strategy
        elif route.strategy is None:
            route.strategy = MockStrategy.URL_OVERRIDE
            validation.warnings.append(f"mock_injection_missing:{route.dep_id}")
        elif route.strategy is MockStrategy.UNSUPPORTED:
            raise MockPlanError(
                "mock_injection_invalid",
                f"{route.dep_id}: unsupported 必须提供 injection.reason",
            )
    return validation


def _injection_model(
    route: MockPlanRoute,
    injection: MockPlanInjection,
    *,
    endpoint: str,
    route_key: str,
) -> MockInjection:
    if injection.strategy is MockStrategy.URL_OVERRIDE:
        injected = f"{endpoint}/_dep/{route_key}"
        if injection.kind == "endpoint_url":
            if route.path_key != "/":
                injected += route.path_key
    elif injection.strategy in (
        MockStrategy.DISCOVERY_OVERRIDE,
        MockStrategy.HTTP_PROXY,
    ):
        injected = endpoint
    else:
        injected = ""
    return MockInjection(
        dep_id=injection.dep_id,
        strategy=injection.strategy,
        property=injection.property,
        kind=injection.kind,
        original=injection.original or "",
        injected=injected,
        path_mode=injection.path_mode,
        reason=injection.reason,
    )


@dataclass
class MockPlanRuntime:
    """已启动的内嵌 Mock 服务及其计划元数据。"""

    server: MockHttpServer
    plan: MockPlan
    plan_path: Path
    route_keys: dict[str, str]
    injections: list[MockInjection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def evidence_path(self) -> Path | None:
        return self.server.evidence.path

    @property
    def token(self) -> str:
        return self.server.token

    def _port(self) -> int:
        return urlsplit(self.server.endpoint).port or 0

    def manifest(self, *, run_id: str) -> dict[str, Any]:
        model = self.server.server_model()
        return {
            "schema_version": 1,
            "run_id": run_id,
            "sut_id": self.plan.sut_id,
            "namespace": self.plan.namespace,
            "mode": "embedded",
            "plan_path": str(self.plan_path),
            "host": self.server.host,
            "port": self._port(),
            "endpoint": self.server.endpoint,
            "admin_endpoint": self.server.admin_endpoint,
            "token_fingerprint": model.token_fingerprint,
            "history_path": str(self.evidence_path) if self.evidence_path else None,
            "route_count": model.route_count,
            "routes": [route.model_dump(mode="json") for route in model.routes],
            "injections": [
                injection.model_dump(mode="json", exclude_none=True)
                for injection in self.injections
            ],
            "warnings": list(self.warnings),
        }

    def injection_document(self) -> dict[str, Any]:
        """供外部 SUT 启动器消费的最小注入清单。"""
        unsupported = [
            injection
            for injection in self.injections
            if injection.strategy is MockStrategy.UNSUPPORTED
        ]
        return {
            "schema_version": 1,
            "sut_id": self.plan.sut_id,
            "namespace": self.plan.namespace,
            "endpoint": self.server.endpoint,
            "injections": [
                injection.model_dump(mode="json", exclude_none=True)
                for injection in self.injections
            ],
            "unsupported_count": len(unsupported),
            "warnings": list(self.warnings),
        }

    def report_state(self) -> dict[str, Any]:
        return {
            "mode": "embedded",
            "plan": str(self.plan_path),
            "sut_id": self.plan.sut_id,
            "namespace": self.plan.namespace,
            "endpoint": self.server.endpoint,
            "route_count": len(self.plan.routes),
            "injection_count": len(self.injections),
            "unsupported_count": sum(
                1
                for injection in self.injections
                if injection.strategy is MockStrategy.UNSUPPORTED
            ),
            "warnings": list(self.warnings),
        }

    def stop(self) -> None:
        self.server.stop()


def _build_runtime(plan: MockPlan, *, plan_path: Path, history_path: str | Path | None):
    validation = validate_mock_plan(plan)
    server = MockHttpServer(
        sut_id=plan.sut_id,
        namespace=plan.namespace,
        host=plan.host,
        port=plan.port,
        history_path=history_path,
        capture_bodies=plan.capture_bodies,
    )
    route_by_dep = {route.dep_id: route for route in plan.routes}
    try:
        for route in plan.routes:
            assert route.route_key is not None
            server.add_route(
                MockRoute(
                    route_key=route.route_key,
                    dep_id=route.dep_id,
                    method=route.method,
                    path_key=route.path_key,
                    host=route.host,
                    target=route.target,
                    strategy=route.strategy or MockStrategy.URL_OVERRIDE,
                    source=route.source,
                ),
                dict(route.branches),
            )
        runtime = MockPlanRuntime(
            server=server,
            plan=plan,
            plan_path=plan_path,
            route_keys=dict(validation.route_keys),
            warnings=list(validation.warnings),
        )
        server.start()
    except Exception:
        server.stop()
        raise
    # 只有 start() 之后端口才确定，因此注入 URL 必须在启动后计算。
    runtime.injections = [
        _injection_model(
            route_by_dep[injection.dep_id],
            injection,
            endpoint=server.endpoint,
            route_key=validation.route_keys[injection.dep_id],
        )
        for injection in plan.injections
    ]
    return runtime


def start_mock_plan(
    plan_path: str | Path,
    *,
    history_path: str | Path | None = None,
) -> MockPlanRuntime:
    """加载计划、构建路由表并启动内嵌服务；失败时保证不留下半启动实例。"""
    resolved = Path(plan_path).resolve()
    plan = load_mock_plan(resolved)
    return _build_runtime(plan, plan_path=resolved, history_path=history_path)


__all__ = [
    "LOOPBACK_HOSTS",
    "MAX_PLAN_BYTES",
    "MAX_PLAN_ROUTES",
    "MockPlanError",
    "MockPlanRuntime",
    "MockPlanValidation",
    "load_mock_plan",
    "resolve_plan_path",
    "start_mock_plan",
    "validate_mock_plan",
]
