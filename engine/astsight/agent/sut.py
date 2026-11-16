"""被测系统（SUT）启动/就绪/停止，以及从 Mock 注入清单生成环境变量。

支持 SUT 启动期注入：环境变量形式的 URL override、明文 HTTP 代理、
Spring Cloud 静态服务列表（`discovery_override`，写入 `SPRING_APPLICATION_JSON`），
以及 Java agent 改写（`agent_rewrite`，通过 `JAVA_TOOL_OPTIONS` 加载）。
配置文件、DNS 改写不在本模块能力内，遇到就显式告警，不假装成功。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import httpx

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_URL_KEYS = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
_AGENT_JAR_ENV = "ASTSIGHT_JAVA_AGENT_JAR"
_AGENT_TOKEN_ENV = "ASTSIGHT_AGENT_TOKEN"
_AGENT_JAR_DEFAULT = (
    Path(__file__).resolve().parents[3]
    / "java"
    / "astsight-agent"
    / "target"
    / "astsight-agent.jar"
)


@dataclass
class SutProcess:
    proc: subprocess.Popen
    base_url: str
    log_path: Path
    env_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def injection_env(
    document: dict[str, Any],
    *,
    base_url: str,
    extra_env: dict[str, str] | None = None,
    agent_token: str | None = None,
) -> tuple[dict[str, str], list[str], list[str]]:
    """返回 (env, env_keys, warnings)。

    admin token 默认不进入环境；仅当存在 `agent_rewrite` 注入时，才通过
    独立环境变量交给进程内的 Java agent（避免出现在命令行/日志里）。
    """
    env: dict[str, str] = dict(extra_env or {})
    keys: list[str] = sorted(env)
    warnings: list[str] = []
    discovery: dict[str, list[dict[str, str]]] = {}
    agent_rewrite: list[str] = []
    agent_endpoint = ""
    dep_hosts = {
        str(route.get("dep_id") or ""): str(route.get("host") or "")
        for route in document.get("routes") or []
        if isinstance(route, dict) and route.get("dep_id")
    }
    for injection in document.get("injections") or []:
        strategy = str(injection.get("strategy") or "")
        injected = str(injection.get("injected") or "")
        prop = str(injection.get("property") or "")
        if strategy == "url_override":
            if not injected:
                warnings.append(f"injection_missing_url:{injection.get('dep_id')}")
                continue
            if not prop:
                warnings.append(f"injection_missing_property:{injection.get('dep_id')}")
                continue
            if not _ENV_KEY_RE.match(prop):
                warnings.append(
                    f"injection_property_not_env:{prop}"
                )
                continue
            env[prop] = injected
            keys.append(prop)
        elif strategy == "http_proxy":
            if not injected:
                warnings.append(f"injection_missing_url:{injection.get('dep_id')}")
                continue
            proxy_keys = sorted(_URL_KEYS)
            for key in proxy_keys:
                if key.startswith("no_"):
                    continue
                env[key.upper()] = injected
                env[key] = injected
                keys.extend([key.upper(), key])
            host = ""
            try:
                host = httpx.URL(base_url).host or ""
            except Exception:
                pass
            bypass = ",".join(filter(None, ["127.0.0.1", "localhost", host]))
            env["NO_PROXY"] = bypass
            env["no_proxy"] = bypass
            keys.extend(["NO_PROXY", "no_proxy"])
        elif strategy == "discovery_override":
            dep_id = str(injection.get("dep_id") or "")
            # 优先用注入里保留原始大小写的服务名；路由表里的 host 已被归一化。
            service = str(injection.get("service_name") or "") or dep_hosts.get(
                dep_id, ""
            )
            if not injected:
                warnings.append(f"injection_missing_url:{dep_id}")
                continue
            if not service:
                warnings.append(f"discovery_not_supported:{dep_id}")
                continue
            discovery[service] = [{"uri": injected}]
        elif strategy == "agent_rewrite":
            dep_id = str(injection.get("dep_id") or "")
            if not injected:
                warnings.append(f"injection_missing_url:{dep_id}")
                continue
            if not agent_endpoint:
                agent_endpoint = injected
            agent_rewrite.append(dep_id)
        elif strategy == "unsupported":
            warnings.append(
                f"injection_unsupported:{injection.get('dep_id')}:"
                f"{injection.get('reason') or 'unsupported'}"
            )
        else:
            warnings.append(f"injection_strategy_unknown:{strategy}")
    if discovery:
        env, keys, warnings = _merge_discovery_env(env, keys, warnings, discovery)
    if agent_rewrite:
        env, keys, warnings = _merge_agent_env(
            env,
            keys,
            warnings,
            agent_rewrite,
            endpoint=str(document.get("endpoint") or "") or agent_endpoint,
            sut_id=str(document.get("sut_id") or ""),
            token=agent_token,
        )
    return env, sorted(set(keys)), warnings


def _agent_jar() -> Path | None:
    configured = os.environ.get(_AGENT_JAR_ENV, "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    return _AGENT_JAR_DEFAULT if _AGENT_JAR_DEFAULT.is_file() else None


def _merge_agent_env(
    env: dict[str, str],
    keys: list[str],
    warnings: list[str],
    dep_ids: list[str],
    *,
    endpoint: str,
    sut_id: str,
    token: str | None,
) -> tuple[dict[str, str], list[str], list[str]]:
    """把 `agent_rewrite` 注入落成 `JAVA_TOOL_OPTIONS` + token 环境变量。

    token 只走环境变量（agent 的 `token_env` 选项），不写进 javaagent 参数，
    避免出现在进程命令行和 JVM 的 `Picked up JAVA_TOOL_OPTIONS` 日志中。
    """
    jar = _agent_jar()
    if jar is None:
        for dep_id in dep_ids:
            warnings.append(
                f"injection_agent_unavailable:{dep_id}:agent_jar_missing"
            )
        return env, keys, warnings
    if not endpoint:
        for dep_id in dep_ids:
            warnings.append(f"injection_agent_unavailable:{dep_id}:endpoint_missing")
        return env, keys, warnings
    secret = (token or os.environ.get(_AGENT_TOKEN_ENV) or "").strip()
    if not secret:
        for dep_id in dep_ids:
            warnings.append(
                f"injection_agent_unavailable:{dep_id}:token_missing"
            )
        return env, keys, warnings
    if any(char.isspace() for char in str(jar)):
        for dep_id in dep_ids:
            warnings.append(
                f"injection_agent_unavailable:{dep_id}:agent_jar_path_has_space"
            )
        return env, keys, warnings

    options = [f"admin={endpoint}", f"token_env={_AGENT_TOKEN_ENV}"]
    if sut_id:
        options.append(f"sut_id={sut_id}")
    javaagent = f"-javaagent:{jar}=" + ",".join(options)
    existing = (
        env.get("JAVA_TOOL_OPTIONS")
        or os.environ.get("JAVA_TOOL_OPTIONS")
        or ""
    ).strip()
    env["JAVA_TOOL_OPTIONS"] = (
        f"{existing} {javaagent}".strip() if existing else javaagent
    )
    env[_AGENT_TOKEN_ENV] = secret
    keys.extend(["JAVA_TOOL_OPTIONS", _AGENT_TOKEN_ENV])
    for dep_id in dep_ids:
        warnings.append(
            f"injection_agent_rewrite_applied:{dep_id}:requires_java_agent"
        )
    return env, keys, warnings


def _merge_discovery_env(
    env: dict[str, str],
    keys: list[str],
    warnings: list[str],
    discovery: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, str], list[str], list[str]]:
    """把静态服务实例合并进 SPRING_APPLICATION_JSON，不覆盖用户已有配置。"""
    raw = env.get("SPRING_APPLICATION_JSON")
    if raw is None:
        raw = os.environ.get("SPRING_APPLICATION_JSON")
    document: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            warnings.append("discovery_not_supported:spring_application_json_invalid")
            return env, keys, warnings
        document = parsed
    node = document
    for key in ("spring", "cloud", "discovery", "client", "simple"):
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    existing = node.get("instances")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(discovery)
    node["instances"] = merged
    env["SPRING_APPLICATION_JSON"] = json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    )
    keys.append("SPRING_APPLICATION_JSON")
    for service in sorted(discovery):
        warnings.append(
            f"injection_discovery_applied:{service}:"
            "requires_spring_cloud_static_instances"
        )
    return env, keys, warnings


def start_sut(
    argv: Sequence[str],
    *,
    base_url: str,
    log_path: str | Path,
    injection_document: dict[str, Any],
    extra_env: dict[str, str] | None = None,
    agent_token: str | None = None,
    ready_url: str | None = None,
    timeout: float = 60.0,
    cwd: str | Path | None = None,
) -> SutProcess:
    if not argv:
        raise ValueError("SUT 启动命令为空")
    env, keys, warnings = injection_env(
        injection_document,
        base_url=base_url,
        extra_env=extra_env,
        agent_token=agent_token,
    )
    full_env = {**os.environ, **env}
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("wb")
    try:
        proc = subprocess.Popen(
            list(argv),
            env=full_env,
            cwd=str(cwd) if cwd else None,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        handle.close()
        raise
    sut = SutProcess(
        proc=proc, base_url=base_url, log_path=path, env_keys=keys, warnings=warnings
    )
    handle.close()
    target = ready_url or f"{base_url.rstrip('/')}/health"
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"SUT 启动失败（exit={proc.returncode}）：{_tail(path)}"
            )
        try:
            response = httpx.get(target, timeout=1.0)
            if response.status_code < 500:
                return sut
        except httpx.HTTPError:
            time.sleep(0.2)
    sut.stop()
    raise TimeoutError(f"SUT 就绪超时: {target}\n{_tail(path)}")


def _tail(path: Path, limit: int = 4_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


__all__ = ["SutProcess", "injection_env", "start_sut"]
