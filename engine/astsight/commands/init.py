"""`astsight init`：生成默认模型配置。

产品默认路径是“装好即可配置模型”：不要求用户复制示例文件或理解所有字段。
配置文件只保存 provider/model/base_url 与 key 的环境变量名，不写入明文 key。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_OPENAI_MODEL = "gpt-4.1"
DEFAULT_KEY_ENV = "ASTSIGHT_LLM_API_KEY"
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_TOKENS = 16384


def default_config_path(*, local: bool = False, home: Path | None = None) -> Path:
    if local:
        return Path.cwd() / "llm.json"
    base = Path.home() if home is None else home
    return base / ".astsight" / "llm.json"


def detect_defaults(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """从现有环境变量推断最省事的默认值。"""
    values = os.environ if env is None else env
    if values.get("ANTHROPIC_AUTH_TOKEN"):
        return {
            "provider": "anthropic",
            "model": values.get("ANTHROPIC_MODEL") or DEFAULT_MODEL,
            "base_url": values.get("ANTHROPIC_BASE_URL") or "",
            "auth_style": "bearer",
            "api_key_env": "ANTHROPIC_AUTH_TOKEN",
        }
    if values.get("ANTHROPIC_API_KEY"):
        return {
            "provider": "anthropic",
            "model": values.get("ANTHROPIC_MODEL") or DEFAULT_MODEL,
            "base_url": values.get("ANTHROPIC_BASE_URL") or "",
            "auth_style": "x-api-key",
            "api_key_env": "ANTHROPIC_API_KEY",
        }
    if values.get("OPENAI_API_KEY"):
        return {
            "provider": "openai",
            "model": values.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
            "base_url": values.get("OPENAI_BASE_URL") or "",
            "auth_style": "bearer",
            "api_key_env": "OPENAI_API_KEY",
        }
    return {
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
        "base_url": "",
        "auth_style": "x-api-key",
        "api_key_env": DEFAULT_KEY_ENV,
    }


def _ask(label: str, default: str) -> str:
    prompt = f"{label} [{default}]: " if default else f"{label}: "
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def initialize(
    *,
    out: Path | None = None,
    local: bool = False,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    auth_style: str | None = None,
    api_key_env: str | None = None,
    force: bool = False,
    interactive: bool = True,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    values = os.environ if env is None else env
    detected = detect_defaults(values)
    provider = (provider or detected["provider"]).strip().lower()
    model = (model or detected["model"]).strip()
    base_url = (base_url if base_url is not None else detected["base_url"]).strip()
    auth_style = (auth_style or detected["auth_style"]).strip().lower()
    api_key_env = (api_key_env or detected["api_key_env"]).strip()

    if interactive and sys.stdin.isatty() and not values.get("CI"):
        provider = _ask("模型协议 provider (anthropic/openai)", provider).lower()
        model = _ask("模型名称", model)
        base_url = _ask("API 地址（可空，使用默认）", base_url)
        api_key_env = _ask("API key 的环境变量名", api_key_env)
        if provider == "anthropic":
            auth_style = "bearer" if api_key_env == "ANTHROPIC_AUTH_TOKEN" else "x-api-key"
        else:
            auth_style = "bearer"

    if provider not in {"anthropic", "openai"}:
        raise ValueError(f"provider 必须是 anthropic 或 openai，实际为 {provider!r}")
    if auth_style not in {"x-api-key", "bearer"}:
        raise ValueError(f"auth_style 必须是 x-api-key 或 bearer，实际为 {auth_style!r}")
    if not model:
        raise ValueError("模型名称不能为空")
    if not api_key_env:
        raise ValueError("API key 环境变量名不能为空")

    path = Path(out) if out else default_config_path(local=local, home=home)
    if path.exists() and not force:
        raise FileExistsError(
            f"配置文件已存在: {path}；如需覆盖请加 --force"
        )
    payload = {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "auth_style": auth_style,
        "api_key": "",
        "api_key_env": api_key_env,
        "timeout": DEFAULT_TIMEOUT,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "temperature": 0.0,
        "max_retries": 1,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    key_present = bool(values.get(api_key_env))
    return {
        "path": str(path),
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "auth_style": auth_style,
        "api_key_env": api_key_env,
        "key_present": key_present,
        "next_steps": _next_steps(path, api_key_env, key_present),
    }


def _next_steps(path: Path, api_key_env: str, key_present: bool) -> list[str]:
    steps = []
    if not key_present:
        steps.append(f"export {api_key_env}=<你的 key>")
    steps.append("astsight codegraph status  # 确认变更追溯运行时可用")
    steps.append(
        "astsight agent run --project <项目> --requirements <需求文档>"
    )
    return steps


__all__ = [
    "DEFAULT_KEY_ENV",
    "DEFAULT_MODEL",
    "default_config_path",
    "detect_defaults",
    "initialize",
]
