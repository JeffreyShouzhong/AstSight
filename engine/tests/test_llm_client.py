"""模型客户端：配置、协议解析、JSON 提取与密钥脱敏。"""
from __future__ import annotations

import json

import httpx
import pytest

from astsight.llm import (
    LLMClient,
    LLMConfig,
    LLMConfigError,
    LLMError,
    config_from_env,
    extract_json,
)


def _client(handler, **overrides) -> LLMClient:
    config = LLMConfig(
        provider=overrides.pop("provider", "openai"),
        api_key=overrides.pop("api_key", "sk-test-secret"),
        model="test-model",
        base_url=overrides.pop("base_url", "https://api.example.test/v1"),
        **overrides,
    )
    return LLMClient(config, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_config_from_env_infers_provider_and_rejects_missing_key() -> None:
    config = config_from_env(
        {
            "ASTSIGHT_LLM_API_KEY": "sk-ant-abc",
            "ASTSIGHT_LLM_MODEL": "claude-test",
        }
    )
    assert config.provider == "anthropic"
    assert config.base_url == "https://api.anthropic.com"
    assert "api_key" not in config.redacted()
    with pytest.raises(LLMConfigError):
        config_from_env({"ASTSIGHT_LLM_MODEL": "m"})


def test_extract_json_strips_fences_and_finds_object() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('说明文字 [1, 2] 结束') == [1, 2]
    with pytest.raises(LLMError):
        extract_json("not json")


def test_openai_protocol_and_usage() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    client = _client(handler)
    payload = client.complete_json(system="sys", user="usr")
    assert payload == {"ok": True}
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer sk-test-secret"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert client.usage[0].input_tokens == 11
    assert client.usage[0].output_tokens == 7


def test_anthropic_protocol_collects_text_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") == "sk-ant-test"
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": '{"part":'},
                    {"type": "text", "text": '"a"}'},
                ],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    client = _client(
        handler, provider="anthropic", api_key="sk-ant-test",
        base_url="https://api.anthropic.test",
    )
    assert client.complete_json(system="s", user="u") == {"part": "a"}


def test_http_error_redacts_api_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key sk-test-secret")

    client = _client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete(system="s", user="u")
    assert "sk-test-secret" not in str(exc.value)
    assert "***" in str(exc.value)


def test_prompt_and_response_size_limits() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = _client(handler, max_prompt_chars=10)
    with pytest.raises(LLMError) as exc:
        client.complete(system="s", user="u" * 20)
    assert exc.value.code == "llm_prompt_too_large"

    client = _client(
        lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "x" * 100}}], "usage": {}},
        ),
        max_response_bytes=32,
    )
    with pytest.raises(LLMError) as exc:
        client.complete(system="s", user="u")
    assert exc.value.code == "llm_response_too_large"
