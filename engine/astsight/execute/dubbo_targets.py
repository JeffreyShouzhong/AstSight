"""Dubbo 直连目标解析。

测试必须钉到具体的 SUT provider；注册中心只说明“存在可用实例”，不能证明
返回地址属于本次被测部署，因此这里不接注册中心，也不允许注册中心兜底。

解析顺序（对应能力设计 3.3）：

1. 校验代码来源与运行 SUT 一致；
2. 代码 / XML 显式地址；
3. 按 protocol + IP + port 去重；
4. 服务级目标映射（显式映射优先于通用直连地址）；
5. 运行配置的 SUT 直连地址；
6. 仍不唯一则阻断。

只接受 IPv4 / IPv6 字面量和显式端口；域名、K8s Service、VIP 一律拒绝。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

SUPPORTED_SCHEMES = {"dubbo", "tri", "grpc"}


class DubboTargetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TargetEndpoint:
    scheme: str
    host: str
    port: int

    @property
    def url(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def identity(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.port)


@dataclass(frozen=True)
class DubboService:
    key: str
    interface: str
    group: str
    version: str
    protocol: str
    serialization: str
    compatibility: str
    direct_url: str | None
    protocol_port: int | None
    file: str
    line: int

    @property
    def allowed_schemes(self) -> set[str]:
        if self.protocol == "dubbo":
            return {"dubbo"}
        if self.protocol == "grpc":
            return {"grpc"}
        if self.protocol == "tri":
            if (
                self.compatibility == "grpc_compatible"
                or self.serialization.lower() in {"protobuf", "proto"}
            ):
                return {"tri", "grpc"}
            # Triple-native 走 sidecar，sidecar 接受 tri:// 直连。
            return {"tri"}
        return set(SUPPORTED_SCHEMES)

    @property
    def default_scheme(self) -> str:
        if self.protocol == "grpc":
            return "grpc"
        if self.protocol == "tri":
            return "tri"
        return "dubbo"


@dataclass
class DubboTargetConfig:
    direct_endpoint: str | None = None
    global_url: str | None = None
    direct_host: str | None = None
    service_targets: dict[str, str | dict[str, Any]] = field(default_factory=dict)
    source_sha256: str | None = None
    source_commit: str | None = None
    source_branch: str | None = None
    artifact_path: str | None = None
    allow_override: bool = False
    sut_id: str | None = None


@dataclass
class DubboTargetResolution:
    ok: bool
    targets: dict[str, str]
    services: list[dict[str, Any]]
    errors: list[dict[str, str]]
    warnings: list[str]
    source: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "targets": self.targets,
            "services": self.services,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    @property
    def error_text(self) -> str:
        return "; ".join(
            f"{item['code']}:{item.get('service_key') or '-'}:{item['message']}"
            for item in self.errors
        )


def normalize_service_key(interface: str, group: str | None, version: str | None) -> str:
    query: list[str] = []
    if group:
        query.append(f"group={group}")
    if version:
        query.append(f"version={version}")
    return interface + ("?" + "&".join(query) if query else "")


def _service_key_from_mapping(raw: str) -> str:
    text = str(raw or "").strip()
    if "?" not in text:
        return text
    interface, _, query = text.partition("?")
    values: dict[str, str] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key not in {"group", "version"}:
            raise DubboTargetError(
                "dubbo_target_config_invalid",
                f"服务键只支持 group/version，收到 {key!r}",
            )
        values[key] = value
    return normalize_service_key(
        interface.strip(), values.get("group"), values.get("version")
    )


def _parse_endpoint(
    value: str | dict[str, Any],
    *,
    allowed_schemes: Iterable[str],
    default_scheme: str,
) -> TargetEndpoint:
    allowed = {item.lower() for item in allowed_schemes}
    if isinstance(value, dict):
        host = str(value.get("host") or "").strip()
        port = value.get("port")
        scheme = str(value.get("scheme") or default_scheme).lower()
    else:
        text = str(value or "").strip()
        if not text:
            raise DubboTargetError("dubbo_target_invalid", "目标地址为空")
        if "://" not in text:
            text = f"{default_scheme}://{text}"
            parsed = urlsplit(text)
            scheme = default_scheme
            host = parsed.hostname or ""
            port = parsed.port
        else:
            parsed = urlsplit(text)
            scheme = parsed.scheme.lower()
            host = parsed.hostname or ""
            port = parsed.port
    if scheme not in allowed:
        raise DubboTargetError(
            "dubbo_target_invalid",
            f"目标协议 {scheme} 与服务不匹配，允许 {sorted(allowed)}",
        )
    if not host:
        raise DubboTargetError("dubbo_target_invalid", "目标地址缺少主机")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise DubboTargetError(
            "dubbo_target_invalid",
            f"只接受 IPv4/IPv6 字面量，收到 {host!r}（域名/VIP/K8s Service 不可用）",
        ) from exc
    if port is None or not 1 <= int(port) <= 65535:
        raise DubboTargetError("dubbo_target_invalid", "目标地址缺少合法端口")
    return TargetEndpoint(scheme=scheme, host=str(ip), port=int(port))


def _config_from_payload(payload: dict[str, Any]) -> DubboTargetConfig:
    source = payload.get("source") or {}
    if source and not isinstance(source, dict):
        raise DubboTargetError(
            "dubbo_target_config_invalid", "source 必须是对象"
        )
    targets = payload.get("targets") or payload.get("service_targets") or {}
    if not isinstance(targets, dict):
        raise DubboTargetError(
            "dubbo_target_config_invalid", "targets 必须是对象"
        )
    return DubboTargetConfig(
        direct_endpoint=payload.get("direct_endpoint"),
        global_url=payload.get("global_url"),
        direct_host=payload.get("direct_host"),
        service_targets=dict(targets),
        source_sha256=(source.get("sha256") or payload.get("source_sha256")),
        source_commit=(source.get("commit") or payload.get("source_commit")),
        source_branch=(source.get("branch") or payload.get("source_branch")),
        artifact_path=(source.get("artifact") or payload.get("artifact")),
        allow_override=bool(
            payload.get("allow_override", payload.get("allow_target_override", False))
        ),
        sut_id=payload.get("sut_id"),
    )


def load_target_config(path: str | Path, *, project: str | Path | None = None) -> DubboTargetConfig:
    raw = Path(path)
    if not raw.is_absolute():
        candidate = Path.cwd() / raw
        if candidate.is_file():
            raw = candidate
        elif project is not None:
            raw = Path(project) / raw
    if not raw.is_file():
        raise DubboTargetError(
            "dubbo_target_config_missing", f"目标映射文件不存在：{raw}"
        )
    try:
        payload = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DubboTargetError(
            "dubbo_target_config_invalid", f"目标映射文件无法解析：{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DubboTargetError(
            "dubbo_target_config_invalid", "目标映射文件必须是 JSON 对象"
        )
    return _config_from_payload(payload)


def merge_config(base: DubboTargetConfig, override: DubboTargetConfig) -> DubboTargetConfig:
    """CLI 覆盖文件；只覆盖显式给出的字段。"""
    return DubboTargetConfig(
        direct_endpoint=override.direct_endpoint or base.direct_endpoint,
        global_url=override.global_url or base.global_url,
        direct_host=override.direct_host or base.direct_host,
        service_targets={**base.service_targets, **override.service_targets},
        source_sha256=override.source_sha256 or base.source_sha256,
        source_commit=override.source_commit or base.source_commit,
        source_branch=override.source_branch or base.source_branch,
        artifact_path=override.artifact_path or base.artifact_path,
        allow_override=override.allow_override or base.allow_override,
        sut_id=override.sut_id or base.sut_id,
    )


def config_from_run(run_config: Any, *, project: str | Path) -> DubboTargetConfig:
    """把 RunConfig / FlowConfig 转成目标配置；文件优先，CLI 覆盖。"""
    file_config = (
        load_target_config(run_config.dubbo_targets, project=project)
        if getattr(run_config, "dubbo_targets", None)
        else DubboTargetConfig()
    )
    cli_config = DubboTargetConfig(
        direct_endpoint=getattr(run_config, "dubbo_target", None),
        global_url=getattr(run_config, "dubbo_url", None),
        direct_host=getattr(run_config, "dubbo_host", None),
        source_sha256=getattr(run_config, "dubbo_source_sha256", None),
        source_commit=getattr(run_config, "dubbo_source_commit", None),
        source_branch=getattr(run_config, "dubbo_source_branch", None),
        artifact_path=getattr(run_config, "dubbo_artifact", None),
        allow_override=bool(
            getattr(run_config, "dubbo_target_override", False)
        ),
    )
    return merge_config(file_config, cli_config)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _git(project: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip() or None


def verify_source(project: Path, config: DubboTargetConfig) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """校验运行 SUT 与画像来源一致；未提供来源时只告警。"""
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    record: dict[str, Any] = {
        "provided": bool(
            config.source_sha256 or config.source_commit or config.source_branch
        ),
        "verified": False,
    }
    if not record["provided"]:
        warnings.append(
            "dubbo_source_unverified: 未提供 SUT 构建 sha256 或 commit/branch，"
            "无法证明运行实例与画像来源一致"
        )
        return record, errors, warnings

    if config.source_commit or config.source_branch:
        actual_commit = _git(project, "rev-parse", "HEAD")
        actual_branch = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
        record["actual_commit"] = actual_commit
        record["actual_branch"] = actual_branch
        if actual_commit is None:
            errors.append(
                {
                    "code": "dubbo_source_unavailable",
                    "message": f"{project} 不是可读取的 git 仓库，无法校验来源",
                }
            )
        else:
            if config.source_commit and config.source_commit != actual_commit:
                errors.append(
                    {
                        "code": "dubbo_source_mismatch",
                        "message": (
                            f"commit {config.source_commit} != {actual_commit}"
                        ),
                    }
                )
            if config.source_branch and config.source_branch != actual_branch:
                errors.append(
                    {
                        "code": "dubbo_source_mismatch",
                        "message": (
                            f"branch {config.source_branch} != {actual_branch}"
                        ),
                    }
                )

    if config.source_sha256:
        if not config.artifact_path:
            errors.append(
                {
                    "code": "dubbo_source_artifact_missing",
                    "message": "提供 source.sha256 时必须同时给出 artifact 路径",
                }
            )
        else:
            artifact = Path(config.artifact_path)
            if not artifact.is_absolute():
                artifact = project / artifact
            if not artifact.is_file():
                errors.append(
                    {
                        "code": "dubbo_source_artifact_missing",
                        "message": f"构建产物不存在：{artifact}",
                    }
                )
            else:
                actual = _sha256_file(artifact)
                record["actual_sha256"] = actual
                if actual != config.source_sha256.lower():
                    errors.append(
                        {
                            "code": "dubbo_source_mismatch",
                            "message": (
                                f"artifact sha256 {config.source_sha256} != {actual}"
                            ),
                        }
                    )
    record["verified"] = not errors
    return record, errors, warnings


def _collect_services(profiles: list[dict[str, Any]]) -> tuple[list[DubboService], list[dict[str, str]], list[str]]:
    services: dict[str, DubboService] = {}
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    for profile in profiles:
        transport = str(profile.get("transport") or "").lower()
        if transport != "dubbo":
            continue
        service = str(profile.get("service") or "").strip()
        if not service:
            continue
        group = str(profile.get("group") or "")
        version = str(profile.get("version") or "")
        key = normalize_service_key(service, group, version)
        protocol = str(profile.get("protocol") or transport or "dubbo").lower()
        serialization = str(profile.get("serialization") or "").lower()
        if not serialization:
            serialization = (
                "protobuf" if protocol in {"tri", "grpc"} else "hessian2"
            )
        item = DubboService(
            key=key,
            interface=service,
            group=group,
            version=version,
            protocol=protocol,
            serialization=serialization,
            compatibility=str(profile.get("compatibility") or "native"),
            direct_url=str(profile.get("direct_url") or "").strip() or None,
            protocol_port=profile.get("protocol_port"),
            file=str(profile.get("file") or ""),
            line=int(profile.get("line") or 0),
        )
        existing = services.get(key)
        if existing is not None and (
            existing.protocol != item.protocol
            or existing.serialization != item.serialization
            or (existing.direct_url and item.direct_url and existing.direct_url != item.direct_url)
        ):
            errors.append(
                {
                    "code": "dubbo_target_ambiguous",
                    "service_key": key,
                    "message": "同一服务键出现多个协议/序列化/显式地址",
                }
            )
        services.setdefault(key, item)
    return list(services.values()), errors, warnings


def _find_mapping(
    key: str,
    services: list[DubboService],
) -> tuple[DubboService | None, str | None]:
    exact = next((item for item in services if item.key == key), None)
    if exact is not None:
        return exact, None
    if "?" not in key:
        candidates = [item for item in services if item.interface == key]
        if len(candidates) == 1 and not candidates[0].group and not candidates[0].version:
            return candidates[0], None
        if len(candidates) == 1:
            return None, f"接口 {key} 有 group/version，必须写完整服务键"
        if len(candidates) > 1:
            return None, f"接口 {key} 对应多个 group/version，必须写完整服务键"
    return None, None


def resolve_dubbo_targets(
    profiles: list[dict[str, Any]],
    *,
    config: DubboTargetConfig,
    project: str | Path,
) -> DubboTargetResolution:
    project_path = Path(project)
    source, errors, warnings = verify_source(project_path, config)
    services, service_errors, service_warnings = _collect_services(profiles)
    errors.extend(service_errors)
    warnings.extend(service_warnings)

    targets: dict[str, TargetEndpoint] = {}
    target_sources: dict[str, str] = {}
    service_by_key = {item.key: item for item in services}

    # 2. 代码 / XML 显式地址
    for service in services:
        if not service.direct_url:
            continue
        try:
            targets[service.key] = _parse_endpoint(
                service.direct_url,
                allowed_schemes=service.allowed_schemes,
                default_scheme=service.default_scheme,
            )
            target_sources[service.key] = "code"
        except DubboTargetError as exc:
            errors.append(
                {
                    "code": exc.code,
                    "service_key": service.key,
                    "message": exc.message,
                }
            )

    # 3. 代码地址收敛到同一 endpoint 时，未解析服务共享该 endpoint
    unique_code = {endpoint.identity for endpoint in targets.values()}
    if len(unique_code) == 1:
        shared = next(iter(targets.values()))
        for service in services:
            if service.key in targets:
                continue
            if shared.scheme in service.allowed_schemes:
                targets[service.key] = shared
                target_sources[service.key] = "code_shared"

    # 5. 服务级映射（先于运行直连，便于用户显式指定）
    mapping_errors: list[dict[str, str]] = []
    for raw_key, raw_value in config.service_targets.items():
        try:
            key = _service_key_from_mapping(raw_key)
        except DubboTargetError as exc:
            mapping_errors.append(
                {"code": exc.code, "message": str(exc)}
            )
            continue
        service, find_error = _find_mapping(key, services)
        if find_error:
            mapping_errors.append(
                {
                    "code": "dubbo_target_ambiguous",
                    "service_key": key,
                    "message": find_error,
                }
            )
            continue
        if service is None:
            warnings.append(f"dubbo_target_unknown_service: {raw_key}")
            continue
        try:
            mapped = _parse_endpoint(
                raw_value,
                allowed_schemes=service.allowed_schemes,
                default_scheme=service.default_scheme,
            )
        except DubboTargetError as exc:
            mapping_errors.append(
                {
                    "code": exc.code,
                    "service_key": service.key,
                    "message": exc.message,
                }
            )
            continue
        current = targets.get(service.key)
        if current is not None and current.identity != mapped.identity:
            if not config.allow_override:
                mapping_errors.append(
                    {
                        "code": "dubbo_target_override_required",
                        "service_key": service.key,
                        "message": "代码地址与运行映射不一致；需显式允许目标覆盖",
                    }
                )
                continue
            warnings.append(
                f"dubbo_target_override: {service.key} "
                f"{current.url} -> {mapped.url}"
            )
            target_sources[service.key] = "mapping_override"
        else:
            target_sources[service.key] = "mapping"
        targets[service.key] = mapped
    errors.extend(mapping_errors)

    # 4. 运行配置的 SUT 直连地址（只补未解析服务）
    if config.direct_endpoint:
        for service in services:
            if service.key in targets:
                continue
            try:
                direct = _parse_endpoint(
                    config.direct_endpoint,
                    allowed_schemes=service.allowed_schemes,
                    default_scheme=service.default_scheme,
                )
            except DubboTargetError as exc:
                errors.append(
                    {
                        "code": exc.code,
                        "service_key": service.key,
                        "message": exc.message,
                    }
                )
                continue
            targets[service.key] = direct
            target_sources[service.key] = "direct"
    elif config.direct_host:
        try:
            host = str(ipaddress.ip_address(config.direct_host.strip()))
        except ValueError:
            errors.append(
                {
                    "code": "dubbo_target_invalid",
                    "message": (
                        f"direct_host 只接受 IPv4/IPv6 字面量，收到 "
                        f"{config.direct_host!r}"
                    ),
                }
            )
        else:
            for service in services:
                if service.key in targets:
                    continue
                if service.protocol_port is None:
                    continue
                targets[service.key] = TargetEndpoint(
                    scheme=service.default_scheme,
                    host=host,
                    port=int(service.protocol_port),
                )
                target_sources[service.key] = "direct_host"

    # 全局 URL 只能用于“所有服务同一个 endpoint”的场景，不能兜底多个 endpoint
    if config.global_url:
        try:
            global_endpoint = _parse_endpoint(
                config.global_url,
                allowed_schemes=SUPPORTED_SCHEMES,
                default_scheme="dubbo",
            )
        except DubboTargetError as exc:
            errors.append({"code": exc.code, "message": exc.message})
        else:
            existing = {endpoint.identity for endpoint in targets.values()}
            if existing and existing != {global_endpoint.identity}:
                errors.append(
                    {
                        "code": "dubbo_global_url_ambiguous",
                        "message": (
                            "已存在多个 endpoint，全局 Dubbo URL 不得作为兜底"
                        ),
                    }
                )
            else:
                for service in services:
                    if global_endpoint.scheme not in service.allowed_schemes:
                        errors.append(
                            {
                                "code": "dubbo_target_invalid",
                                "service_key": service.key,
                                "message": (
                                    f"全局 URL 协议 {global_endpoint.scheme} "
                                    f"与服务不匹配"
                                ),
                            }
                        )
                        continue
                    targets.setdefault(service.key, global_endpoint)
                    target_sources.setdefault(service.key, "global")

    for service in services:
        if service.key not in targets:
            errors.append(
                {
                    "code": "dubbo_target_unresolved",
                    "service_key": service.key,
                    "message": (
                        "没有代码地址、运行直连地址或服务映射；"
                        "注册中心不作为兜底"
                    ),
                }
            )

    records: list[dict[str, Any]] = []
    url_targets: dict[str, str] = {}
    for service in services:
        endpoint = targets.get(service.key)
        record = {
            "service_key": service.key,
            "interface": service.interface,
            "group": service.group,
            "version": service.version,
            "protocol": service.protocol,
            "serialization": service.serialization,
            "compatibility": service.compatibility,
            "file": service.file,
            "line": service.line,
            "source": target_sources.get(service.key, "unresolved"),
            "determinism": "exact" if endpoint is not None else "unresolved",
            "endpoint": endpoint.url if endpoint else None,
        }
        records.append(record)
        if endpoint is not None:
            url_targets[service.key] = endpoint.url

    return DubboTargetResolution(
        ok=not errors,
        targets=url_targets,
        services=records,
        errors=errors,
        warnings=_dedupe(warnings),
        source=source,
    )


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "DubboService",
    "DubboTargetConfig",
    "DubboTargetError",
    "DubboTargetResolution",
    "TargetEndpoint",
    "config_from_run",
    "load_target_config",
    "merge_config",
    "normalize_service_key",
    "resolve_dubbo_targets",
    "verify_source",
]
