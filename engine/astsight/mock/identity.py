"""C3.5.5 下游身份：`dep_id` / `route_key` 的确定性构建。

本模块不启动服务、不读写文件，只做纯函数计算；C3 画像与测试都复用它。
规则见 `framework-capability-design.md` C3.5.5：

- `dep_id = http:{downstream_key} {METHOD} {path_key}`
- `downstream_key` 优先级 `svc:` > `cfg:` > `host:` > `call:`
- 同一下游端点的多个调用点合并，不同目标撞 id 时追加目标哈希
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_COLON_PARAM = re.compile(r"(^|/):[A-Za-z_][A-Za-z0-9_]*")
_SAFE_SVC = re.compile(r"[^a-z0-9._-]+")
_SAFE_CFG = re.compile(r"[^a-z0-9._-]+")
_WS = re.compile(r"\s+")


class DepIdError(ValueError):
    """`dep_id` 无法唯一确定；调用方应阻断画像。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class TlsNotSupportedError(DepIdError):
    def __init__(self, target: str):
        super().__init__("tls_not_supported", target)


@dataclass(frozen=True)
class CallSite:
    """C3 识别出的一个出站 HTTP 调用点。

    `target` 是完整目标 URL（含 scheme/host/base path）。地址无法静态解析时留空，
    由 `call_site_id` 单独成组并标 `inferred`。
    """

    call_site_id: str
    method: str
    target: str | None = None
    path: str | None = None
    service_name: str | None = None
    config_key: str | None = None


@dataclass(frozen=True)
class NormalizedTarget:
    url: str
    host: str
    port: int | None
    path_key: str


@dataclass
class DepIdEntry:
    dep_id: str
    downstream_key: str
    evidence: str  # svc / cfg / host / call
    method: str
    path_key: str
    target: str | None
    targets: list[str] = field(default_factory=list)
    call_sites: list[str] = field(default_factory=list)
    route_host: str | None = None
    inferred: bool = False


@dataclass
class DepCollision:
    dep_id: str
    targets: list[str]
    resolved: bool
    from_id: str
    to_id: str


@dataclass
class UnsupportedCallSite:
    call_site_id: str
    reason: str
    detail: str = ""


@dataclass
class DepIdPlan:
    deps: list[DepIdEntry] = field(default_factory=list)
    collisions: list[DepCollision] = field(default_factory=list)
    unsupported: list[UnsupportedCallSite] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def normalize_method(method: str) -> str:
    return str(method or "").strip().upper()


def normalize_path_key(path: str) -> str:
    """归一化下游完整路径：占位符、query/fragment、重复斜杠、尾斜杠。"""
    value = str(path or "")
    value = value.split("#", 1)[0].split("?", 1)[0]
    value = _PLACEHOLDER.sub("{}", value)
    value = _COLON_PARAM.sub(lambda m: m.group(1) + "{}", value)
    value = re.sub(r"/+", "/", value)
    if not value.startswith("/"):
        value = "/" + value
    if len(value) > 1:
        value = value.rstrip("/")
    return value or "/"


def _bracket_ipv6(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _is_private_or_loopback(host: str) -> bool:
    lowered = host.lower().strip("[]")
    if lowered == "localhost":
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def normalize_host(host: str, port: int | None = None) -> str:
    """规范化 Host 证据；回环/私网地址的非默认端口保留。"""
    value = _bracket_ipv6(str(host or "").strip().lower())
    if port and port != 80 and _is_private_or_loopback(value):
        return f"{value}:{port}"
    return value


def normalize_target_url(target: str) -> NormalizedTarget:
    """解析并规范化目标 URL；仅接受明文 HTTP。"""
    raw = str(target or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise DepIdError("invalid_target", str(exc)) from exc
    scheme = parts.scheme.lower()
    if scheme == "https":
        raise TlsNotSupportedError(raw)
    if scheme != "http":
        raise DepIdError("unsupported_scheme", raw)
    host = (parts.hostname or "").lower()
    if not host:
        raise DepIdError("invalid_target", raw)
    try:
        port = parts.port
    except ValueError as exc:
        raise DepIdError("invalid_target", str(exc)) from exc
    display_host = _bracket_ipv6(host)
    if port and port != 80:
        display_host = f"{display_host}:{port}"
    path_key = normalize_path_key(parts.path or "/")
    return NormalizedTarget(
        url=f"http://{display_host}{path_key}",
        host=normalize_host(host, port),
        port=port,
        path_key=path_key,
    )


def _downstream_key(kind: str, value: str) -> str:
    if kind == "svc":
        cleaned = _SAFE_SVC.sub("-", value.strip().lower()).strip("-")
        return f"svc:{cleaned}"
    if kind == "cfg":
        cleaned = _SAFE_CFG.sub("-", value.strip().lower()).strip("-")
        return f"cfg:{cleaned}"
    if kind == "host":
        # 调用方传入的已是 normalize_host 的结果（可能带端口）。
        return f"host:{value.strip().lower()}"
    call = _WS.sub("", value)
    return f"call:{call}"


def build_dep_id(downstream_key: str, method: str, path_key: str) -> str:
    normalized = normalize_method(method)
    if not normalized:
        raise DepIdError("http_method_missing")
    return f"http:{downstream_key} {normalized} {normalize_path_key(path_key)}"


def route_key_for(dep_id: str, length: int = 12) -> str:
    if length not in (12, 16, 20):
        raise ValueError("route_key length must be 12, 16 or 20")
    return "r" + hashlib.sha256(dep_id.encode("utf-8")).hexdigest()[:length]


def _target_hash(target: str | None, call_site: str) -> str:
    basis = target or f"call:{call_site}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8]


def _pick_identity(
    sites: list[CallSite], target: NormalizedTarget | None
) -> tuple[str, str, list[str]]:
    """返回 (kind, downstream_key, conflicts)。

    没有 normalized target 的调用点按 C3.5.5 只能进 `call:` 组，即使画像
    碰巧知道服务名，也不能把“地址未解析”当成稳定下游身份。
    """
    if target is None:
        return "call", _downstream_key("call", sites[0].call_site_id), []
    candidates: list[tuple[int, str, str]] = []
    for site in sites:
        if site.service_name:
            candidates.append((1, "svc", site.service_name))
        if site.config_key:
            candidates.append((2, "cfg", site.config_key))
    candidates.append((3, "host", target.host))
    best_priority = min(item[0] for item in candidates)
    kind = next(item[1] for item in candidates if item[0] == best_priority)
    keys = sorted(
        {
            _downstream_key(kind, item[2])
            for item in candidates
            if item[0] == best_priority
        }
    )
    conflicts = keys[1:] if len(keys) > 1 else []
    return kind, keys[0], conflicts


def _group_key(
    site: CallSite, method: str, target: NormalizedTarget | None, path_key: str
) -> tuple:
    if target is not None:
        return ("target", method, target.url)
    return ("call", method, path_key, site.call_site_id)


def build_dep_id_plan(call_sites: list[CallSite]) -> DepIdPlan:
    """按 C3.5.5 构建唯一 `dep_id` 清单。

    无法静态确定目标地址的调用点会成组并标 `inferred`；HTTPS 进入
    `unsupported`（`tls_not_supported`），不参与 Mock。
    """
    plan = DepIdPlan()
    groups: dict[tuple, list[tuple[CallSite, NormalizedTarget | None, str]]] = {}

    for site in call_sites:
        method = normalize_method(site.method)
        if not method:
            plan.unsupported.append(
                UnsupportedCallSite(site.call_site_id, "http_method_missing")
            )
            continue
        target: NormalizedTarget | None = None
        if site.target:
            try:
                target = normalize_target_url(site.target)
            except TlsNotSupportedError as exc:
                plan.unsupported.append(
                    UnsupportedCallSite(site.call_site_id, "tls_not_supported", exc.detail)
                )
                continue
            except DepIdError as exc:
                plan.unsupported.append(
                    UnsupportedCallSite(site.call_site_id, exc.code, exc.detail)
                )
                continue
        if target is None and not site.path:
            plan.unsupported.append(
                UnsupportedCallSite(site.call_site_id, "target_unresolved")
            )
            continue
        path_key = target.path_key if target else normalize_path_key(site.path or "/")
        key = _group_key(site, method, target, path_key)
        groups.setdefault(key, []).append((site, target, path_key))

    by_dep: dict[str, list[DepIdEntry]] = {}
    for entries in groups.values():
        sites = [entry[0] for entry in entries]
        target = entries[0][1]
        path_key = entries[0][2]
        method = normalize_method(sites[0].method)
        kind, downstream_key, conflicts = _pick_identity(sites, target)
        if conflicts:
            plan.warnings.append(
                f"dep_evidence_conflict:{downstream_key}:{','.join(conflicts)}"
            )
        dep_id = build_dep_id(downstream_key, method, path_key)
        targets = sorted({entry[1].url for entry in entries if entry[1] is not None})
        route_host = None
        if kind == "svc":
            route_host = downstream_key.split(":", 1)[1]
        elif target is not None:
            route_host = target.host
        entry = DepIdEntry(
            dep_id=dep_id,
            downstream_key=downstream_key,
            evidence=kind,
            method=method,
            path_key=path_key,
            target=targets[0] if targets else None,
            targets=targets,
            call_sites=[site.call_site_id for site in sites],
            route_host=route_host,
            inferred=target is None,
        )
        by_dep.setdefault(dep_id, []).append(entry)

    for dep_id, entries in by_dep.items():
        if len(entries) == 1:
            plan.deps.append(entries[0])
            continue
        # 同一逻辑服务多实例：服务名、方法、路径一致，目标主机不同 → 合并。
        svc_ids = {entry.downstream_key for entry in entries}
        paths = {(entry.method, entry.path_key) for entry in entries}
        if len(svc_ids) == 1 and entries[0].evidence == "svc" and len(paths) == 1:
            merged = entries[0]
            for extra in entries[1:]:
                merged.targets = sorted(set(merged.targets) | set(extra.targets))
                merged.call_sites.extend(extra.call_sites)
            plan.deps.append(merged)
            continue
        # 不同目标撞同一 dep_id：追加目标哈希，仍冲突则阻断。
        suffixed: dict[str, DepIdEntry] = {}
        for entry in entries:
            resolved_id = f"{dep_id}~{_target_hash(entry.target, entry.call_sites[0])}"
            plan.collisions.append(
                DepCollision(
                    dep_id=dep_id,
                    targets=entry.targets,
                    resolved=True,
                    from_id=dep_id,
                    to_id=resolved_id,
                )
            )
            if resolved_id in suffixed:
                raise DepIdError(
                    "dep_collision",
                    f"{resolved_id} from {dep_id} targets={entry.targets}",
                )
            entry.dep_id = resolved_id
            suffixed[resolved_id] = entry
        plan.deps.extend(suffixed.values())

    plan.deps.sort(key=lambda item: item.dep_id)
    plan.unsupported.sort(key=lambda item: item.call_site_id)
    return plan


def assign_route_keys(deps: list[DepIdEntry]) -> dict[str, str]:
    """为每个 dep 生成 namespace 内唯一的 route_key。"""
    for length in (12, 16, 20):
        keys = {dep.dep_id: route_key_for(dep.dep_id, length) for dep in deps}
        if len(set(keys.values())) == len(keys):
            return keys
    raise DepIdError("mock_route_conflict", "route_key collision after 20 hex chars")
