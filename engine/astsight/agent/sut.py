"""被测系统（SUT）启动/就绪/停止，以及从 Mock 注入清单生成环境变量。

只支持 SUT 启动期注入：环境变量形式的 URL override / 明文 HTTP 代理。
配置文件、注册中心、DNS 改写不在本模块能力内，遇到就显式告警，不假装成功。
"""
from __future__ import annotations

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
) -> tuple[dict[str, str], list[str], list[str]]:
    """返回 (env, env_keys, warnings)。不把 admin token 传给 SUT。"""
    env: dict[str, str] = dict(extra_env or {})
    keys: list[str] = sorted(env)
    warnings: list[str] = []
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
        elif strategy == "unsupported":
            warnings.append(
                f"injection_unsupported:{injection.get('dep_id')}:"
                f"{injection.get('reason') or 'unsupported'}"
            )
        else:
            warnings.append(f"injection_strategy_unknown:{strategy}")
    return env, sorted(set(keys)), warnings


def start_sut(
    argv: Sequence[str],
    *,
    base_url: str,
    log_path: str | Path,
    injection_document: dict[str, Any],
    extra_env: dict[str, str] | None = None,
    ready_url: str | None = None,
    timeout: float = 60.0,
    cwd: str | Path | None = None,
) -> SutProcess:
    if not argv:
        raise ValueError("SUT 启动命令为空")
    env, keys, warnings = injection_env(
        injection_document, base_url=base_url, extra_env=extra_env
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
