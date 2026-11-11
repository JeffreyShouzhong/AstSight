"""模型访问层：OpenAI-compatible / Anthropic Messages，结构化 JSON 输出。

边界：
- API key 只从环境变量或未提交的本地 `llm.json` 读取，不写入日志、报告或异常文本；
- 只接受有限大小的请求/响应，调用次数由上层流程严格控制；
- 模型输出永远是不可信输入，必须由调用方再做 schema/证据校验。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import httpx

DEFAULT_MAX_PROMPT_CHARS = 400_000
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0
# 推理型模型（如 deepseek-flash）会先输出 thinking 再输出正文，
# 8192 常被思考阶段耗尽，导致没有任何 text block。默认给足一个结构化 JSON 的预算。
DEFAULT_MAX_TOKENS = 16_384

_PROVIDERS = {"openai", "anthropic"}
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)
# 配置文件给用户直接编辑；模板见 engine/llm.example.json。
DEFAULT_CONFIG_FILE_NAME = "llm.json"
_CONFIG_FILE_KEYS = {
    "schema_version",
    "provider",
    "model",
    "base_url",
    "auth_style",
    "api_key",
    "api_key_env",
    "timeout",
    "max_tokens",
    "temperature",
    "max_retries",
}
_STR_CONFIG_KEYS = ("provider", "model", "base_url", "auth_style", "api_key", "api_key_env")
_NUMBER_CONFIG_KEYS = ("timeout", "temperature")
_INT_CONFIG_KEYS = ("max_tokens", "max_retries")


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
    # Anthropic 协议有两种鉴权头：SDK 的 api_key → x-api-key，
    # Claude Code 的 ANTHROPIC_AUTH_TOKEN → Authorization: Bearer。
    auth_style: str = "x-api-key"
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
            raise LLMConfigError(
                "llm_api_key_missing",
                "未配置模型 key：llm.json 的 api_key/api_key_env、ASTSIGHT_LLM_API_KEY、"
                "ANTHROPIC_AUTH_TOKEN、ANTHROPIC_API_KEY 均为空",
            )
        self.model = str(self.model or "").strip()
        if not self.model:
            raise LLMConfigError(
                "llm_model_missing",
                "未配置模型：ASTSIGHT_LLM_MODEL / ANTHROPIC_MODEL 均为空",
            )
        self.base_url = str(self.base_url or "").strip().rstrip("/")
        if not self.base_url:
            self.base_url = (
                _ANTHROPIC_DEFAULT_BASE
                if self.provider == "anthropic"
                else _OPENAI_DEFAULT_BASE
            )
        self.auth_style = str(self.auth_style or "x-api-key").strip().lower()
        if self.auth_style not in {"x-api-key", "bearer"}:
            raise LLMConfigError("llm_auth_style_invalid", self.auth_style)
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
            "auth_style": self.auth_style,
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


def resolve_config_path(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Path | None:
    """定位模型配置文件。

    显式参数与 `ASTSIGHT_LLM_CONFIG` 必须存在（否则报错，避免静默用了错的配置）；
    自动发现按 `./llm.json` → `~/.astsight/llm.json` 的顺序，找不到返回 None。
    """
    values = os.environ if env is None else env
    explicit = str(config_path or "").strip() or str(
        values.get("ASTSIGHT_LLM_CONFIG") or ""
    ).strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise LLMConfigError("llm_config_missing", f"配置文件不存在: {path}")
        return path
    cwd_path = Path.cwd() if cwd is None else Path(cwd)
    home_path = Path.home() if home is None else Path(home)
    candidates = (
        cwd_path / DEFAULT_CONFIG_FILE_NAME,
        home_path / ".astsight" / DEFAULT_CONFIG_FILE_NAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config_file(path: str | Path) -> dict[str, Any]:
    """读取并校验 `llm.json`，返回未脱敏的字段字典。"""
    resolved = Path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LLMConfigError(
            "llm_config_unreadable", f"{resolved}: {exc.strerror or exc}"
        ) from exc
    except ValueError as exc:
        raise LLMConfigError(
            "llm_config_invalid", f"{resolved}: 不是合法 JSON（{exc}）"
        ) from exc
    if not isinstance(raw, dict):
        raise LLMConfigError("llm_config_invalid", f"{resolved}: 顶层必须是对象")
    version = raw.get("schema_version", 1)
    if version != 1:
        raise LLMConfigError(
            "llm_config_version_unsupported",
            f"{resolved}: schema_version={version!r}，仅支持 1",
        )
    unknown = sorted(set(raw) - _CONFIG_FILE_KEYS)
    if unknown:
        raise LLMConfigError(
            "llm_config_unknown_keys",
            f"{resolved}: 未知字段 {unknown}；允许字段 {sorted(_CONFIG_FILE_KEYS)}",
        )
    for name in _STR_CONFIG_KEYS:
        value = raw.get(name)
        if value is not None and not isinstance(value, str):
            raise LLMConfigError(
                "llm_config_invalid", f"{resolved}: {name} 必须是字符串"
            )
    for name in _NUMBER_CONFIG_KEYS:
        value = raw.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise LLMConfigError("llm_config_invalid", f"{resolved}: {name} 必须是数字")
    for name in _INT_CONFIG_KEYS:
        value = raw.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise LLMConfigError("llm_config_invalid", f"{resolved}: {name} 必须是整数")
    return raw


def _file_value(
    file_values: Mapping[str, Any], name: str, default: Any
) -> Any:
    value = file_values.get(name)
    return default if value is None else value


def _build_config(
    values: Mapping[str, str], file_values: Mapping[str, Any]
) -> LLMConfig:
    """环境变量优先、配置文件兜底；key 可由 `api_key_env` 间接读取。"""
    provider = str(values.get("ASTSIGHT_LLM_PROVIDER") or "").strip().lower() or str(
        file_values.get("provider") or ""
    ).strip().lower()

    key = str(values.get("ASTSIGHT_LLM_API_KEY") or "").strip()
    key_source = "env" if key else ""
    if not key:
        key = str(file_values.get("api_key") or "").strip()
        key_source = "file" if key else ""
    api_key_env = str(file_values.get("api_key_env") or "").strip()
    if not key and api_key_env:
        key = str(values.get(api_key_env) or "").strip()
        key_source = "file_env" if key else ""
    if not key:
        key = str(values.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
        key_source = "anthropic_auth_token" if key else ""
    if not key:
        key = str(values.get("ANTHROPIC_API_KEY") or "").strip()
        key_source = "anthropic_api_key" if key else ""

    from_anthropic_env = key_source in {"anthropic_auth_token", "anthropic_api_key"}
    bearer_from_file = key_source == "file_env" and api_key_env == "ANTHROPIC_AUTH_TOKEN"
    if not provider:
        provider = (
            "anthropic"
            if from_anthropic_env or bearer_from_file or key.startswith("sk-ant-")
            else "openai"
        )
    anthropic_defaults = provider == "anthropic"
    model = (
        str(values.get("ASTSIGHT_LLM_MODEL") or "").strip()
        or str(file_values.get("model") or "").strip()
        or (
            str(values.get("ANTHROPIC_MODEL") or "").strip()
            if anthropic_defaults
            else ""
        )
    )
    base_url = (
        str(values.get("ASTSIGHT_LLM_BASE_URL") or "").strip()
        or str(file_values.get("base_url") or "").strip()
        or (
            str(values.get("ANTHROPIC_BASE_URL") or "").strip()
            if anthropic_defaults
            else ""
        )
    )
    auth_style = (
        str(values.get("ASTSIGHT_LLM_AUTH_STYLE") or "").strip()
        or str(file_values.get("auth_style") or "").strip()
    )
    if not auth_style:
        auth_style = (
            "bearer"
            if key_source == "anthropic_auth_token" or bearer_from_file
            else "x-api-key"
        )
    return LLMConfig(
        provider=provider,
        api_key=key,
        model=model,
        base_url=base_url,
        auth_style=auth_style,
        timeout=_env_float(
            values,
            "ASTSIGHT_LLM_TIMEOUT",
            float(_file_value(file_values, "timeout", DEFAULT_TIMEOUT)),
        ),
        max_tokens=_env_int(
            values,
            "ASTSIGHT_LLM_MAX_TOKENS",
            int(_file_value(file_values, "max_tokens", DEFAULT_MAX_TOKENS)),
        ),
        temperature=_env_float(
            values,
            "ASTSIGHT_LLM_TEMPERATURE",
            float(_file_value(file_values, "temperature", 0.0)),
        ),
        max_retries=_env_int(
            values,
            "ASTSIGHT_LLM_MAX_RETRIES",
            int(_file_value(file_values, "max_retries", 1)),
        ),
    )


def config_from_env(env: Mapping[str, str] | None = None) -> LLMConfig:
    """只从环境变量构造配置（不读取配置文件，保持测试与 CI 的确定性）。

    优先读 `ASTSIGHT_LLM_*`；未设置时兼容 Claude Code / Anthropic 兼容端点的
    通用变量（如 DeepSeek 的 `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`）：
    key → `ANTHROPIC_AUTH_TOKEN` > `ANTHROPIC_API_KEY`；
    model → `ANTHROPIC_MODEL`；base_url → `ANTHROPIC_BASE_URL`。
    未显式设置 provider 时，来自 Anthropic 变量或 `sk-ant-` 前缀的 key 判为 anthropic。
    """
    values = os.environ if env is None else env
    return _build_config(values, {})


def load_config(
    *,
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> LLMConfig:
    """从配置文件 + 环境变量构造配置，供 CLI 使用。

    查找顺序见 `resolve_config_path`；环境变量 `ASTSIGHT_LLM_*` 覆盖文件值，
    因此 CI 可以用环境变量覆盖本地 `llm.json` 而不必改动文件。
    """
    values = os.environ if env is None else env
    path = resolve_config_path(config_path, env=values)
    file_values = load_config_file(path) if path is not None else {}
    return _build_config(values, file_values)


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
            auth = (
                {"authorization": f"Bearer {self.config.api_key}"}
                if self.config.auth_style == "bearer"
                else {"x-api-key": self.config.api_key}
            )
            return {
                **auth,
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
            # 推理型模型会把 max_tokens 先花在 thinking 上：这不是“空响应”，
            # 而是预算不够。两者必须区分，否则只会误导人去重试同一个配置。
            truncated = payload.get("stop_reason") == "max_tokens" or any(
                isinstance(choice, dict) and choice.get("finish_reason") == "length"
                for choice in payload.get("choices") or []
            )
            if truncated:
                raise LLMError(
                    "llm_response_truncated",
                    f"模型在输出正文前用尽 max_tokens={self.config.max_tokens}；"
                    "提高 ASTSIGHT_LLM_MAX_TOKENS 后重试",
                )
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
    "DEFAULT_CONFIG_FILE_NAME",
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
    "load_config",
    "load_config_file",
    "resolve_config_path",
]
