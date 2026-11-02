"""模型访问层：OpenAI-compatible / Anthropic Messages，结构化 JSON 输出。

边界：
- API key 只从环境变量读取，不写入日志、报告或异常文本；
- 只接受有限大小的请求/响应，调用次数由上层流程严格控制；
- 模型输出永远是不可信输入，必须由调用方再做 schema/证据校验。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

DEFAULT_MAX_PROMPT_CHARS = 400_000
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 8_192

_PROVIDERS = {"openai", "anthropic"}
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


class LLMError(RuntimeError):
    """模型调用或输出解析失败；错误文本包含 key 时也必须先脱敏。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class LLMConfigError(LLMError):
    """缺少模型配置。"""


@dataclass
class LLMConfig:
    provider: str
    api_key: str = field(repr=False)
    model: str
    base_url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.0
    max_retries: int = 1
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        self.provider = str(self.provider or "").strip().lower()
        if self.provider not in _PROVIDERS:
            raise LLMConfigError(
                "llm_provider_invalid",
                f"provider 必须是 {sorted(_PROVIDERS)}，实际为 {self.provider!r}",
            )
        self.api_key = str(self.api_key or "").strip()
        if not self.api_key:
            raise LLMConfigError("llm_api_key_missing", "ASTSIGHT_LLM_API_KEY 为空")
        self.model = str(self.model or "").strip()
        if not self.model:
            raise LLMConfigError("llm_model_missing", "ASTSIGHT_LLM_MODEL 为空")
        self.base_url = str(self.base_url or "").strip().rstrip("/")
        if not self.base_url:
            self.base_url = (
                _ANTHROPIC_DEFAULT_BASE
                if self.provider == "anthropic"
                else _OPENAI_DEFAULT_BASE
            )
        if self.timeout <= 0:
            raise LLMConfigError("llm_timeout_invalid", str(self.timeout))
        if self.max_tokens <= 0:
            raise LLMConfigError("llm_max_tokens_invalid", str(self.max_tokens))
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise LLMConfigError("llm_temperature_invalid", str(self.temperature))
        self.max_retries = max(0, min(int(self.max_retries), 3))

    def redacted(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "max_retries": self.max_retries,
        }


@dataclass
class LLMUsage:
    prompt_chars: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_chars": self.prompt_chars,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
        }


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise LLMConfigError("llm_config_invalid", f"{name}={raw!r}") from exc


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise LLMConfigError("llm_config_invalid", f"{name}={raw!r}") from exc


def config_from_env(env: Mapping[str, str] | None = None) -> LLMConfig:
    """从环境变量构造配置；未设置 provider 时按 key 前缀推断。"""
    values = os.environ if env is None else env
    key = str(values.get("ASTSIGHT_LLM_API_KEY") or "").strip()
    provider = str(values.get("ASTSIGHT_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        provider = "anthropic" if key.startswith("sk-ant-") else "openai"
    return LLMConfig(
        provider=provider,
        api_key=key,
        model=str(values.get("ASTSIGHT_LLM_MODEL") or "").strip(),
        base_url=str(values.get("ASTSIGHT_LLM_BASE_URL") or "").strip(),
        timeout=_env_float(values, "ASTSIGHT_LLM_TIMEOUT", DEFAULT_TIMEOUT),
        max_tokens=_env_int(values, "ASTSIGHT_LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        temperature=_env_float(values, "ASTSIGHT_LLM_TEMPERATURE", 0.0),
        max_retries=_env_int(values, "ASTSIGHT_LLM_MAX_RETRIES", 1),
    )


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _endpoint(base_url: str, provider: str) -> str:
    base = base_url.rstrip("/")
    if provider == "anthropic":
        return base if base.endswith("/v1/messages") else f"{base}/v1/messages"
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _strip_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = _FENCE_RE.sub("", value).strip()
    return value


def extract_json(text: str) -> Any:
    """从模型文本中提取一个 JSON 值；失败时抛 llm_invalid_json。"""
    value = _strip_fence(text)
    try:
        return json.loads(value)
    except ValueError:
        pass
    starts = [index for index in (value.find("{"), value.find("[")) if index >= 0]
    if starts:
        start = min(starts)
        end = max(value.rfind("}"), value.rfind("]"))
        if end > start:
            try:
                return json.loads(value[start : end + 1])
            except ValueError:
                pass
    snippet = value[:500].replace("\n", "\\n")
    raise LLMError("llm_invalid_json", f"无法解析 JSON，响应片段: {snippet}")


class LLMClient:
    """同步模型客户端；上层一次流程内复用，便于统计调用次数与 token。"""

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.config = config
        self._sleep = sleep
        self.calls: list[LLMUsage] = []
        self.last_usage: LLMUsage | None = None
        self._client = httpx.Client(
            timeout=httpx.Timeout(config.timeout),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def usage(self) -> list[LLMUsage]:
        return list(self.calls)

    def _headers(self) -> dict[str, str]:
        if self.config.provider == "anthropic":
            return {
                "x-api-key": self.config.api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
        return {
            "authorization": f"Bearer {self.config.api_key}",
            "content-type": "application/json",
        }

    def _payload(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        if self.config.provider == "anthropic":
            return {
                "model": self.config.model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "temperature": self.config.temperature,
                "max_tokens": max_tokens,
            }
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": max_tokens,
        }

    def _response_text(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        usage = payload.get("usage") or {}
        if self.config.provider == "anthropic":
            blocks = payload.get("content") or []
            if not isinstance(blocks, list):
                raise LLMError("llm_response_invalid", "Anthropic content 不是数组")
            text = "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            tokens = (
                int(usage.get("input_tokens") or 0) or None,
                int(usage.get("output_tokens") or 0) or None,
            )
        else:
            choices = payload.get("choices") or []
            if not isinstance(choices, list) or not choices:
                raise LLMError("llm_response_invalid", "OpenAI 响应没有 choices")
            message = (choices[0] or {}).get("message") or {}
            text = message.get("content")
            if isinstance(text, list):
                text = "".join(
                    str(part.get("text") or "")
                    for part in text
                    if isinstance(part, dict)
                )
            tokens = (
                int(usage.get("prompt_tokens") or 0) or None,
                int(usage.get("completion_tokens") or 0) or None,
            )
        if not isinstance(text, str) or not text.strip():
            raise LLMError("llm_response_empty", "模型返回空文本")
        return text, {"input_tokens": tokens[0], "output_tokens": tokens[1]}

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
    ) -> str:
        """返回模型文本。提示词/响应超限、鉴权失败、网络错误都显式失败。"""
        system = str(system or "")
        user = str(user or "")
        total_chars = len(system) + len(user)
        if total_chars > self.config.max_prompt_chars:
            raise LLMError(
                "llm_prompt_too_large",
                f"{total_chars} chars > {self.config.max_prompt_chars}",
            )
        limit = max(1, int(max_tokens or self.config.max_tokens))
        url = _endpoint(self.config.base_url, self.config.provider)
        body = self._payload(system, user, limit)
        usage = LLMUsage(prompt_chars=total_chars)
        started = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            usage.attempts = attempt + 1
            try:
                response = self._client.post(url, headers=self._headers(), json=body)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    self._sleep(0.5 * (attempt + 1))
                    continue
                raise LLMError(
                    "llm_transport_error",
                    _redact(f"{type(exc).__name__}: {exc}", self.config.api_key)[:300],
                ) from exc

            if response.status_code in _RETRYABLE_STATUS and attempt < self.config.max_retries:
                self._sleep(0.5 * (attempt + 1))
                continue
            if response.status_code >= 400:
                detail = _redact(response.text[:500], self.config.api_key)
                raise LLMError(
                    "llm_http_error",
                    f"HTTP {response.status_code}: {detail}",
                )
            if len(response.content) > self.config.max_response_bytes:
                raise LLMError(
                    "llm_response_too_large",
                    f"{len(response.content)} bytes > {self.config.max_response_bytes}",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise LLMError("llm_response_invalid", "响应不是 JSON") from exc
            text, token_info = self._response_text(payload)
            usage.input_tokens = token_info["input_tokens"]
            usage.output_tokens = token_info["output_tokens"]
            usage.latency_ms = int((time.monotonic() - started) * 1000)
            self.calls.append(usage)
            self.last_usage = usage
            return text

        raise LLMError(
            "llm_transport_error",
            _redact(f"{type(last_error).__name__}: {last_error}", self.config.api_key)[:300],
        )

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
    ) -> Any:
        return extract_json(self.complete(system=system, user=user, max_tokens=max_tokens))


__all__ = [
    "DEFAULT_MAX_PROMPT_CHARS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT",
    "LLMClient",
    "LLMConfig",
    "LLMConfigError",
    "LLMError",
    "LLMUsage",
    "config_from_env",
    "extract_json",
]
