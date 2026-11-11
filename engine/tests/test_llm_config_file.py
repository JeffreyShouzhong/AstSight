"""模型配置文件：定位、校验、与平台环境变量的合并优先级。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astsight.llm import (
    DEFAULT_CONFIG_FILE_NAME,
    LLMConfigError,
    load_config,
    load_config_file,
    resolve_config_path,
)


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_resolve_config_path_prefers_explicit_then_env(tmp_path: Path) -> None:
    explicit = _write(tmp_path / "explicit.json", {"api_key": "k", "model": "m"})
    env_file = _write(tmp_path / "env.json", {"api_key": "k2", "model": "m2"})
    assert resolve_config_path(explicit, env={}) == explicit
    assert (
        resolve_config_path(None, env={"ASTSIGHT_LLM_CONFIG": str(env_file)})
        == env_file
    )
    with pytest.raises(LLMConfigError) as caught:
        resolve_config_path(tmp_path / "missing.json", env={})
    assert caught.value.code == "llm_config_missing"


def test_resolve_config_path_discovers_cwd_then_home(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    (home / ".astsight").mkdir(parents=True)
    home_file = _write(
        home / ".astsight" / DEFAULT_CONFIG_FILE_NAME, {"api_key": "k", "model": "m"}
    )
    assert resolve_config_path(None, env={}, cwd=cwd, home=home) == home_file
    cwd_file = _write(cwd / DEFAULT_CONFIG_FILE_NAME, {"api_key": "k", "model": "m"})
    assert resolve_config_path(None, env={}, cwd=cwd, home=home) == cwd_file


def test_load_config_reads_file_and_env_overrides_it(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "llm.json",
        {
            "provider": "openai",
            "model": "file-model",
            "base_url": "https://file.example.test/v1",
            "api_key": "file-key",
            "timeout": 30,
            "max_tokens": 4096,
        },
    )
    config = load_config(env={"ASTSIGHT_LLM_MODEL": "env-model"}, config_path=path)
    assert config.provider == "openai"
    assert config.model == "env-model"  # 环境变量优先
    assert config.base_url == "https://file.example.test/v1"
    assert config.api_key == "file-key"
    assert config.timeout == 30
    assert config.max_tokens == 4096


def test_load_config_reads_key_through_api_key_env(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "llm.json",
        {
            "provider": "anthropic",
            "model": "claude-test",
            "api_key_env": "MY_LLM_TOKEN",
        },
    )
    config = load_config(env={"MY_LLM_TOKEN": "secret-token"}, config_path=path)
    assert config.api_key == "secret-token"
    assert config.provider == "anthropic"
    assert config.base_url == "https://api.anthropic.com"


def test_load_config_infers_provider_from_anthropic_env(tmp_path: Path) -> None:
    path = _write(tmp_path / "llm.json", {"api_key_env": "ANTHROPIC_AUTH_TOKEN"})
    config = load_config(
        env={
            "ANTHROPIC_AUTH_TOKEN": "token",
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_MODEL": "deepseek-flash",
        },
        config_path=path,
    )
    assert config.provider == "anthropic"
    assert config.base_url == "https://api.deepseek.com/anthropic"
    assert config.model == "deepseek-flash"
    assert config.auth_style == "bearer"


def test_load_config_rejects_unknown_keys_and_bad_json(tmp_path: Path) -> None:
    unknown = _write(tmp_path / "unknown.json", {"api_key": "k", "modle": "typo"})
    with pytest.raises(LLMConfigError) as caught:
        load_config_file(unknown)
    assert caught.value.code == "llm_config_unknown_keys"
    assert "modle" in caught.value.detail

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(LLMConfigError) as caught_json:
        load_config_file(broken)
    assert caught_json.value.code == "llm_config_invalid"

    wrong_version = _write(tmp_path / "v2.json", {"schema_version": 2})
    with pytest.raises(LLMConfigError) as caught_version:
        load_config_file(wrong_version)
    assert caught_version.value.code == "llm_config_version_unsupported"


def test_shipped_template_is_valid_and_has_no_secret() -> None:
    template = Path(__file__).resolve().parents[1] / "llm.example.json"
    payload = load_config_file(template)
    assert payload["schema_version"] == 1
    assert payload["api_key"] == ""
    assert payload["api_key_env"]
