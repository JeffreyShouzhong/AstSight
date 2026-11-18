"""Mock 运行计划的加载、校验与内嵌服务生命周期（C3.5.5 P0）。

计划由 C3 画像或人工编写；本模块把它变成可启动的 `MockHttpServer`，
并生成不含 token 的 `run_manifest.json` / `mock_injection.json` 形态。
"""
from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from contracts import (
    DubboMockLocator,
    GrpcMockLocator,
    HttpMockLocator,
    MockCall,
    MockInjection,
    MockPlan,
    MockPlanDep,
    MockPlanDubboRoute,
    MockPlanInjection,
    MockPlanRoute,
    MockRoute,
    MockSession,
    MockStrategy,
    Transport,
)

from .identity import (
    DepIdEntry,
    TlsNotSupportedError,
    assign_route_keys,
    normalize_method,
    normalize_path_key,
    normalize_target_url,
    parse_dubbo_dep_id,
)
from .server import MockHttpServer
from .grpc_descriptor import GrpcDescriptorError, GrpcDescriptorIndex
from .grpc_runtime import GripMockBackend, GripMockProcess, GrpcMockRuntimeAdapter
from .runtime import (
    EmbeddedMockRuntimeAdapter,
    MockRuntimeManager,
    RuntimeSessionBridge,
)

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


def _allocate_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


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
    validate_grpc_plan_descriptors(plan, base_dir=plan_path.parent)
    return plan


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise MockPlanError(code, detail)


def _normalize_route(
    route: MockPlanRoute, index: int, *, allow_https: bool = False
) -> None:
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
            normalized = normalize_target_url(
                str(target), allow_https=allow_https
            )
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


def _normalize_dubbo_route(
    route: MockPlanDubboRoute, index: int
) -> None:
    where = f"dubbo_routes[{index}]"
    dep_id = str(route.dep_id or "").strip()
    _require(
        len(dep_id) <= MAX_DEP_ID_CHARS,
        "mock_dep_id_invalid",
        f"{where}: dep_id 超过 {MAX_DEP_ID_CHARS} 字符",
    )
    parsed = parse_dubbo_dep_id(dep_id)
    _require(
        parsed is not None,
        "mock_dep_id_invalid",
        f"{where}: 需要规范形态 dubbo:服务#方法(参数类型,...)?group=&version=",
    )
    assert parsed is not None
    _require(
        parsed.service == str(route.service or "").strip()
        and parsed.method == str(route.method or "").strip()
        and list(parsed.parameter_types) == [str(item) for item in route.parameter_types]
        and (parsed.group or None) == (route.group or None)
        and (parsed.version or None) == (route.version or None),
        "mock_dep_id_invalid",
        f"{where}: dep_id 与服务/方法/参数/group/version 不一致",
    )
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
    route.service = parsed.service
    route.method = parsed.method
    route.parameter_types = list(parsed.parameter_types)
    route.group = parsed.group
    route.version = parsed.version


def _materialize_legacy_routes_from_deps(plan: MockPlan) -> None:
    """允许纯 deps[] 新格式进入 Plan；legacy 路由存在时以其为事实源。

    validated Plan 会同时序列化 legacy routes 与派生 deps。为了避免把派生
    deps 误当第二事实源，只要 legacy 路由非空就忽略 deps 输入并由校验重算。
    """
    if plan.routes or plan.dubbo_routes or not plan.deps:
        return
    routes: list[MockPlanRoute] = []
    dubbo_routes: list[MockPlanDubboRoute] = []
    for index, dep in enumerate(plan.deps):
        locator = dep.locator
        if dep.transport is Transport.HTTP:
            _require(
                isinstance(locator, HttpMockLocator),
                "mock_plan_dep_invalid",
                f"deps[{index}]: HTTP transport 需要 HttpMockLocator",
            )
            assert isinstance(locator, HttpMockLocator)
            routes.append(
                MockPlanRoute(
                    dep_id=dep.dep_id,
                    method=locator.method,
                    path_key=locator.path_key,
                    host=locator.host,
                    target=locator.target,
                    strategy=dep.strategy,
                    source=dep.source,
                    route_key=dep.route_key,
                    branches=dict(dep.branches),
                )
            )
        elif dep.transport is Transport.DUBBO:
            _require(
                isinstance(locator, DubboMockLocator),
                "mock_plan_dep_invalid",
                f"deps[{index}]: Dubbo transport 需要 DubboMockLocator",
            )
            assert isinstance(locator, DubboMockLocator)
            dubbo_routes.append(
                MockPlanDubboRoute(
                    dep_id=dep.dep_id,
                    service=locator.service,
                    method=locator.method,
                    parameter_types=list(locator.parameter_types),
                    group=locator.group,
                    version=locator.version,
                    source=dep.source,
                    route_key=dep.route_key,
                    branches=dict(dep.branches),
                )
            )
        elif dep.transport is Transport.GRPC:
            _require(
                isinstance(locator, GrpcMockLocator),
                "mock_plan_dep_invalid",
                f"deps[{index}]: gRPC transport 需要 GrpcMockLocator",
            )
        else:
            raise MockPlanError(
                "mock_plan_dep_invalid",
                f"deps[{index}]: unsupported transport={dep.transport.value}",
            )
    plan.routes = routes
    plan.dubbo_routes = dubbo_routes


def validate_grpc_plan_descriptors(
    plan: MockPlan,
    *,
    base_dir: str | Path | None = None,
) -> int:
    """编译/加载 gRPC descriptor 并校验当前 Plan 的响应体。"""
    root = Path(base_dir).resolve() if base_dir is not None else Path.cwd()
    checked = 0
    for dep in plan.deps:
        if dep.transport is not Transport.GRPC:
            continue
        if not isinstance(dep.locator, GrpcMockLocator):
            raise MockPlanError(
                "mock_plan_dep_invalid",
                f"{dep.dep_id}: 需要 GrpcMockLocator",
            )
        if not dep.locator.descriptor_source:
            raise MockPlanError(
                "mock_descriptor_missing",
                f"{dep.dep_id}: descriptor_source 为空",
            )
        source = Path(dep.locator.descriptor_source).expanduser()
        if not source.is_absolute():
            source = (root / source).resolve()
        try:
            index = GrpcDescriptorIndex.load(source)
            index.method(dep.locator.grpc_path)
            for branch_name, reply in dep.replies.items():
                try:
                    index.validate_response(dep.locator.grpc_path, reply.body)
                except GrpcDescriptorError as exc:
                    raise MockPlanError(
                        exc.code,
                        f"{dep.dep_id}/{branch_name}: {exc.detail}",
                    ) from exc
        except GrpcDescriptorError as exc:
            raise MockPlanError(exc.code, f"{dep.dep_id}: {exc.detail}") from exc
        dep.locator.descriptor_sha256 = index.descriptor_sha256
        checked += 1
    return checked


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

    _materialize_legacy_routes_from_deps(plan)
    grpc_deps = [
        dep for dep in plan.deps if dep.transport is Transport.GRPC
    ]
    _require(
        bool(plan.routes) or bool(plan.dubbo_routes) or bool(grpc_deps),
        "mock_plan_empty",
        "至少需要一条 route",
    )
    _require(
        len(plan.routes) + len(plan.dubbo_routes) + len(grpc_deps)
        <= MAX_PLAN_ROUTES,
        "mock_plan_too_many_routes",
        f"{len(plan.routes) + len(plan.dubbo_routes) + len(grpc_deps)} "
        f"> {MAX_PLAN_ROUTES}",
    )

    validation = MockPlanValidation()
    if host.lower() not in LOOPBACK_HOSTS:
        validation.warnings.append(f"mock_bind_non_loopback:{host}")
    agent_deps = {
        injection.dep_id
        for injection in plan.injections
        if injection.strategy is MockStrategy.AGENT_REWRITE
    }
    seen_deps: set[str] = set()
    deps: list[DepIdEntry] = []
    for index, route in enumerate(plan.routes):
        _normalize_route(
            route,
            index,
            allow_https=(
                route.dep_id in agent_deps
                or route.strategy is MockStrategy.AGENT_REWRITE
            ),
        )
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

    for index, route in enumerate(plan.dubbo_routes):
        _normalize_dubbo_route(route, index)
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
                evidence="dubbo_reference",
                method=route.method,
                path_key="",
                target=None,
            )
        )

    for index, dep in enumerate(grpc_deps):
        where = f"deps[{index}]"
        _require(
            dep.dep_id.startswith("grpc:"),
            "mock_dep_id_invalid",
            f"{where}: gRPC dep_id 必须以 grpc: 开头",
        )
        _require(
            dep.dep_id not in seen_deps,
            "mock_route_conflict",
            f"重复 dep_id: {dep.dep_id}",
        )
        seen_deps.add(dep.dep_id)
        _require(
            isinstance(dep.locator, GrpcMockLocator),
            "mock_plan_dep_invalid",
            f"{where}: 需要 GrpcMockLocator",
        )
        assert isinstance(dep.locator, GrpcMockLocator)
        _require(
            dep.locator.grpc_path.startswith("/")
            and dep.locator.grpc_path.count("/") >= 2,
            "mock_grpc_path_invalid",
            f"{where}: {dep.locator.grpc_path!r}",
        )
        _require(
            len(dep.branches) <= MAX_BRANCHES_PER_ROUTE,
            "mock_plan_too_many_branches",
            f"{where}: {len(dep.branches)} > {MAX_BRANCHES_PER_ROUTE}",
        )
        for name in dep.branches:
            _require(
                bool(str(name).strip()) and len(str(name)) <= MAX_BRANCH_CHARS,
                "mock_branch_invalid",
                f"{where}: 分支名 {name!r}",
            )
            reply = dep.replies.get(name)
            _require(
                reply is not None,
                "mock_reply_required",
                f"{where}: gRPC branch={name!r} 缺少 reply",
            )
            _require(
                reply.transport == "grpc",
                "mock_reply_transport_mismatch",
                f"{where}: gRPC branch={name!r}",
            )
        _require(
            set(dep.replies) == set(dep.branches),
            "mock_branch_invalid",
            f"{where}: branches/replies 名称必须一致",
        )
        if not dep.branches:
            validation.warnings.append(f"mock_branch_empty:{dep.dep_id}")
        deps.append(
            DepIdEntry(
                dep_id=dep.dep_id,
                downstream_key="grpc",
                evidence="grpc_path",
                method=dep.locator.grpc_path.rsplit("/", 1)[-1],
                path_key=dep.locator.grpc_path,
                target=dep.locator.target,
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
    for route in plan.dubbo_routes:
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
    for dep in grpc_deps:
        computed = route_keys[dep.dep_id]
        if dep.route_key is not None and dep.route_key != computed:
            raise MockPlanError(
                "mock_route_key_mismatch",
                f"{dep.dep_id}: {dep.route_key!r} != {computed!r}",
            )
        _require(
            computed not in seen_keys,
            "mock_route_conflict",
            f"重复 route_key: {computed}",
        )
        seen_keys.add(computed)
        dep.route_key = computed
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

    plan.deps = [
        MockPlanDep(
            dep_id=route.dep_id,
            transport=Transport.HTTP,
            route_key=route.route_key,
            strategy=route.strategy,
            source=route.source,
            locator=HttpMockLocator(
                method=route.method,
                path_key=route.path_key,
                host=route.host,
                target=route.target,
            ),
            branches=dict(route.branches),
        )
        for route in plan.routes
    ] + [
        MockPlanDep(
            dep_id=route.dep_id,
            transport=Transport.DUBBO,
            route_key=route.route_key,
            strategy=inject_by_dep.get(route.dep_id).strategy
            if inject_by_dep.get(route.dep_id) is not None
            else None,
            source=route.source,
            locator=DubboMockLocator(
                service=route.service,
                method=route.method,
                parameter_types=list(route.parameter_types),
                group=route.group,
                version=route.version,
            ),
            branches=dict(route.branches),
        )
        for route in plan.dubbo_routes
    ] + [dep.model_copy(deep=True) for dep in grpc_deps]
    return validation


def _injection_model(
    route: MockPlanRoute | MockPlanDubboRoute | MockPlanDep,
    injection: MockPlanInjection,
    *,
    endpoint: str,
    grpc_endpoint: str | None,
    route_key: str,
) -> MockInjection:
    if injection.strategy is MockStrategy.URL_OVERRIDE:
        injected = f"{endpoint}/_dep/{route_key}"
        if injection.kind == "endpoint_url":
            path_key = getattr(route, "path_key", "/")
            if path_key != "/":
                injected += path_key
    elif injection.strategy in (
        MockStrategy.DISCOVERY_OVERRIDE,
        MockStrategy.HTTP_PROXY,
        MockStrategy.AGENT_REWRITE,
    ):
        injected = endpoint
    elif injection.strategy is MockStrategy.GRPC_SERVER:
        injected = grpc_endpoint or ""
    else:
        injected = ""
    return MockInjection(
        dep_id=injection.dep_id,
        strategy=injection.strategy,
        service_name=injection.service_name,
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
    runtimes: MockRuntimeManager = field(default_factory=MockRuntimeManager)
    grpc_endpoint: str | None = None
    grpc_admin_endpoint: str | None = None
    grpc_gateway_endpoint: str | None = None
    grpc_process: GripMockProcess | None = None

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
            "grpc_endpoint": self.grpc_endpoint,
            "grpc_admin_endpoint": self.grpc_admin_endpoint,
            "grpc_gateway_endpoint": self.grpc_gateway_endpoint,
            "token_fingerprint": model.token_fingerprint,
            "history_path": str(self.evidence_path) if self.evidence_path else None,
            "route_count": model.route_count,
            "routes": [route.model_dump(mode="json") for route in model.routes],
            "dubbo_route_count": len(self.plan.dubbo_routes),
            "dubbo_routes": [
                route.model_dump(mode="json") for route in self.plan.dubbo_routes
            ],
            "deps": [
                dep.model_dump(mode="json") for dep in self.plan.deps
            ],
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
            "grpc_endpoint": self.grpc_endpoint,
            "grpc_admin_endpoint": self.grpc_admin_endpoint,
            "grpc_gateway_endpoint": self.grpc_gateway_endpoint,
            # 服务发现型注入需要 route.host（即服务名）才能写静态实例表。
            "routes": [
                {
                    "dep_id": route.dep_id,
                    "host": route.host,
                    "method": route.method,
                    "path_key": route.path_key,
                    "target": route.target,
                }
                for route in self.plan.routes
            ],
            "dubbo_deps": [route.dep_id for route in self.plan.dubbo_routes],
            "deps": [
                dep.model_dump(mode="json") for dep in self.plan.deps
            ],
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
            "grpc_endpoint": self.grpc_endpoint,
            "grpc_admin_endpoint": self.grpc_admin_endpoint,
            "route_count": len(self.plan.routes),
            "dubbo_route_count": len(self.plan.dubbo_routes),
            "dep_count": len(self.plan.deps),
            "injection_count": len(self.injections),
            "unsupported_count": sum(
                1
                for injection in self.injections
                if injection.strategy is MockStrategy.UNSUPPORTED
            ),
            "warnings": list(self.warnings),
        }

    def activate_session(self, session: MockSession) -> None:
        self.runtimes.activate(session)

    def collect_session_calls(self, session: MockSession) -> list[MockCall]:
        return self.runtimes.collect(session)

    def deactivate_session(self, session: MockSession) -> None:
        self.runtimes.deactivate(session)

    def stop(self) -> None:
        runtime_error: Exception | None = None
        try:
            self.runtimes.stop()
        except Exception as exc:
            runtime_error = exc
        finally:
            self.server.stop()
            if self.grpc_process is not None:
                try:
                    self.grpc_process.stop()
                except Exception as exc:
                    if runtime_error is None:
                        runtime_error = exc
        if runtime_error is not None:
            raise runtime_error


def _build_runtime(
    plan: MockPlan,
    *,
    plan_path: Path,
    history_path: str | Path | None,
    grpc_backend: GripMockBackend | None = None,
    gripmock_path: str | Path | None = None,
    grpc_process_factory: Callable[..., GripMockProcess] = GripMockProcess,
    grpc_host: str = "127.0.0.1",
    grpc_ports: tuple[int, int, int] = (0, 0, 0),
    grpc_start_timeout_s: float = 15.0,
):
    validation = validate_mock_plan(plan)
    grpc_deps = [
        dep.dep_id for dep in plan.deps if dep.transport is Transport.GRPC
    ]
    if grpc_deps and grpc_backend is None and gripmock_path is None:
        raise MockPlanError(
            "mock_grpc_runtime_unimplemented",
            ",".join(grpc_deps[:10]),
        )
    if grpc_deps and grpc_backend is not None and gripmock_path is not None:
        raise MockPlanError(
            "mock_grpc_backend_conflict",
            "grpc_backend 与 gripmock_path 只能设置一个",
        )
    server = MockHttpServer(
        sut_id=plan.sut_id,
        namespace=plan.namespace,
        host=plan.host,
        port=plan.port,
        history_path=history_path,
        capture_bodies=plan.capture_bodies,
    )
    grpc_process: GripMockProcess | None = None
    grpc_endpoint: str | None = None
    grpc_admin_endpoint: str | None = None
    grpc_gateway_endpoint: str | None = None
    effective_grpc_backend = grpc_backend
    if grpc_deps and effective_grpc_backend is None:
        assert gripmock_path is not None
        grpc_objects = [
            dep for dep in plan.deps if dep.transport is Transport.GRPC
        ]
        sources: set[Path] = set()
        for dep in grpc_objects:
            assert isinstance(dep.locator, GrpcMockLocator)
            source = Path(str(dep.locator.descriptor_source or "")).expanduser()
            if not source.is_absolute():
                source = (plan_path.parent / source).resolve()
            sources.add(source)
        if len(sources) != 1:
            raise MockPlanError(
                "mock_descriptor_ambiguous",
                f"gripmock 首版要求单一 descriptor source: {sorted(map(str, sources))}",
            )
        grpc_port, admin_port, gateway_port = grpc_ports
        allocated: set[int] = set()
        values: list[int] = []
        for requested in (grpc_port, admin_port, gateway_port):
            port = requested or _allocate_port(grpc_host)
            while port in allocated:
                port = _allocate_port(grpc_host)
            allocated.add(port)
            values.append(port)
        grpc_port, admin_port, gateway_port = values
        grpc_process = grpc_process_factory(
            executable=gripmock_path,
            descriptor_source=next(iter(sources)),
            grpc_port=grpc_port,
            http_port=admin_port,
            gateway_port=gateway_port,
            grpc_host=grpc_host,
            http_host=grpc_host,
            gateway_host=grpc_host,
            work_dir=plan_path.parent,
            start_timeout_s=grpc_start_timeout_s,
        )
        effective_grpc_backend = grpc_process.backend
        grpc_endpoint = f"{grpc_host}:{grpc_port}"
        grpc_admin_endpoint = f"http://{grpc_host}:{admin_port}"
        grpc_gateway_endpoint = f"{grpc_host}:{gateway_port}"
    route_by_dep = {
        route.dep_id: route
        for route in [*plan.routes, *plan.dubbo_routes]
    }
    route_by_dep.update(
        {
            dep.dep_id: dep
            for dep in plan.deps
            if dep.transport is Transport.GRPC
        }
    )
    runtimes = MockRuntimeManager()
    if plan.routes:
        runtimes.register(
            EmbeddedMockRuntimeAdapter(
                server,
                name="http_mock",
                transport=Transport.HTTP,
            )
        )
    if plan.dubbo_routes:
        runtimes.register(
            EmbeddedMockRuntimeAdapter(
                server,
                name="dubbo_filter",
                transport=Transport.DUBBO,
            )
        )
    if grpc_deps:
        assert effective_grpc_backend is not None
        runtimes.register(
            GrpcMockRuntimeAdapter(
                effective_grpc_backend,
                [dep for dep in plan.deps if dep.transport is Transport.GRPC],
            )
        )
    server.set_session_lifecycle(
        RuntimeSessionBridge(runtimes, server.evidence)
    )
    try:
        if grpc_process is not None:
            grpc_process.start()
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
        for route in plan.dubbo_routes:
            assert route.route_key is not None
            server.add_dubbo_dep(
                route.dep_id,
                route_key=route.route_key,
                branches=dict(route.branches),
            )
        runtime = MockPlanRuntime(
            server=server,
            plan=plan,
            plan_path=plan_path,
            route_keys=dict(validation.route_keys),
            warnings=list(validation.warnings),
            runtimes=runtimes,
            grpc_endpoint=grpc_endpoint,
            grpc_admin_endpoint=grpc_admin_endpoint,
            grpc_gateway_endpoint=grpc_gateway_endpoint,
            grpc_process=grpc_process,
        )
        server.start()
        runtimes.start()
    except Exception:
        try:
            runtimes.stop()
        except Exception:
            pass
        server.stop()
        if grpc_process is not None:
            try:
                grpc_process.stop()
            except Exception:
                pass
        raise
    # 只有 start() 之后端口才确定，因此注入 URL 必须在启动后计算。
    runtime.injections = [
        _injection_model(
            route_by_dep[injection.dep_id],
            injection,
            endpoint=server.endpoint,
            grpc_endpoint=grpc_endpoint,
            route_key=validation.route_keys[injection.dep_id],
        )
        for injection in plan.injections
    ]
    injected_deps = {injection.dep_id for injection in runtime.injections}
    for dep in plan.deps:
        if dep.transport is not Transport.GRPC or dep.dep_id in injected_deps:
            continue
        runtime.injections.append(
            MockInjection(
                dep_id=dep.dep_id,
                strategy=MockStrategy.GRPC_SERVER,
                kind="base_url",
                original="",
                injected=grpc_endpoint or "",
                path_mode="replace",
                reason=(
                    "grpc_target_rewrite_required"
                    if grpc_endpoint
                    else "grpc_backend_injected"
                ),
            )
        )
    return runtime


def start_mock_plan(
    plan_path: str | Path,
    *,
    history_path: str | Path | None = None,
    grpc_backend: GripMockBackend | None = None,
    gripmock_path: str | Path | None = None,
    grpc_process_factory: Callable[..., GripMockProcess] = GripMockProcess,
    grpc_host: str = "127.0.0.1",
    grpc_ports: tuple[int, int, int] = (0, 0, 0),
    grpc_start_timeout_s: float = 15.0,
) -> MockPlanRuntime:
    """加载计划、构建路由表并启动内嵌服务；失败时保证不留下半启动实例。"""
    resolved = Path(plan_path).resolve()
    plan = load_mock_plan(resolved)
    return _build_runtime(
        plan,
        plan_path=resolved,
        history_path=history_path,
        grpc_backend=grpc_backend,
        gripmock_path=gripmock_path,
        grpc_process_factory=grpc_process_factory,
        grpc_host=grpc_host,
        grpc_ports=grpc_ports,
        grpc_start_timeout_s=grpc_start_timeout_s,
    )


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
    "validate_grpc_plan_descriptors",
    "validate_mock_plan",
]
