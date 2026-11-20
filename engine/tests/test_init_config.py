"""astsight init：默认模型配置生成与 key 环境变量处理。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astsight.commands.init import detect_defaults, initialize
from astsight.llm.client import load_config


def test_initialize_writes_default_config(tmp_path: Path) -> None:
    target = tmp_path / "llm.json"
    result = initialize(out=target, interactive=False, env={})
    assert result["provider"] == "anthropic"
    assert result["api_key_env"] == "ASTSIGHT_LLM_API_KEY"
    assert result["key_present"] is False
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["model"] == "claude-sonnet-5-5"
    assert payload["api_key"] == ""
    assert payload["api_key_env"] == "ASTSIGHT_LLM_API_KEY"


def test_initialize_detects_anthropic_auth_token(tmp_path: Path) -> None:
    target = tmp_path / "llm.json"
    env = {
        "ANTHROPIC_AUTH_TOKEN": "token",
        "ANTHROPIC_MODEL": "deepseek-chat",
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    }
    defaults = detect_defaults(env)
    assert defaults["provider"] == "anthropic"
    assert defaults["auth_style"] == "bearer"
    result = initialize(out=target, interactive=False, env=env)
    assert result["model"] == "deepseek-chat"
    assert result["base_url"] == "https://api.deepseek.com/anthropic"
    assert result["api_key_env"] == "ANTHROPIC_AUTH_TOKEN"
    assert result["key_present"] is True

    config = load_config(
        env=env, config_path=target
    )
    assert config.model == "deepseek-chat"
    assert config.auth_style == "bearer"


def test_existing_config_requires_force(tmp_path: Path) -> None:
    target = tmp_path / "llm.json"
    initialize(out=target, interactive=False, env={})
    with pytest.raises(FileExistsError):
        initialize(out=target, interactive=False, env={})
    result = initialize(out=target, interactive=False, force=True, env={})
    assert Path(result["path"]) == target


def test_generated_config_is_usable_with_env_key(tmp_path: Path) -> None:
    target = tmp_path / "llm.json"
    initialize(out=target, interactive=False, env={})
    config = load_config(
        env={"ASTSIGHT_LLM_API_KEY": "test-key"},
        config_path=target,
    )
    assert config.provider == "anthropic"
    assert config.api_key == "test-key"
    assert config.model == "claude-sonnet-5-5"


def test_cli_init_yes(tmp_path: Path) -> None:
    from astsight.cli import main

    target = tmp_path / "llm.json"
    assert main(["init", "--yes", "--out", str(target)]) == 0
    assert target.is_file()
