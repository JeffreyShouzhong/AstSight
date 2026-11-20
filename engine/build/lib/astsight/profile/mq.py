"""MQ 生产者调用点扫描：只读源码，识别 topic / exchange / routingKey。

支持字面量、同文件常量、Spring `@Value` / `@ConfigurationProperties` 与
`application*.properties|yml|yaml`、Python `os.getenv` / `os.environ` 与根目录
`.env`。解析不到时显式写 warning，不猜动态值。当前覆盖 Java（Kafka/RabbitMQ/
RocketMQ）与 Python（kafka-python/pika）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MAX_FILES = 2_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PRODUCERS = 200
MAX_CONFIG_FILES = 40
MAX_PLACEHOLDER_DEPTH = 4
_SKIP_DIRS = {
    ".git",
    ".astsight",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
}

_JAVA_STRING = r'"((?:\\.|[^"\\])*)"'
_PY_ASSIGN = re.compile(r"^[ \t]*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$", re.M)
_PY_GETENV = re.compile(
    r"os\.(?:getenv|environ\.get)\s*\(\s*"
    r'["\']([^"\']+)["\']\s*'
    r'(?:,\s*["\']([^"\']*)["\']\s*)?\)'
)
_PY_ENVIRON_INDEX = re.compile(
    r'os\.environ\s*\[\s*["\']([^"\']+)["\']\s*\]'
)
_PY_LITERAL = re.compile(r'^["\']([^"\']*)["\']$')
_JAVA_CONST = re.compile(
    r"(?:static\s+final|final\s+static)\s+String\s+(\w+)\s*=\s*" + _JAVA_STRING
)
_JAVA_VALUE_ANNOTATION = re.compile(
    r"@Value\s*\(\s*(?:value\s*=\s*)?" + _JAVA_STRING + r"\s*\)"
)
_JAVA_VALUE_TARGET = re.compile(
    r"(?:(?:private|protected|public|final|static|transient|volatile)\s+|"
    r"@\w+(?:\s*\([^)]*\))?\s*)*"
    r"[A-Za-z_][\w.<>,\[\]?]*\s+([A-Za-z_]\w*)\s*(?=[;,=,)\]])"
)
_JAVA_CONFIG_PROPERTIES = re.compile(
    r'@ConfigurationProperties\s*\(\s*(?:prefix\s*=\s*)?["\']([^"\']+)["\']'
)
_JAVA_CLASS = re.compile(r"\bclass\s+\w+[^{]*\{")
_JAVA_CONFIG_FIELD = re.compile(
    r"(?:(?:private|protected|public)\s+)"
    r"(?:(?:final|static|volatile|transient)\s+)*"
    r"String\s+(\w+)\s*"
    r"(?:=\s*" + _JAVA_STRING + r")?\s*;"
)
_JAVA_KAFKA_RECORD = re.compile(
    r"new\s+ProducerRecord\s*(?:<[^>]*>)?\s*\(\s*"
    + r'("((?:\\.|[^"\\])*)"|([A-Za-z_]\w*))'
)
_JAVA_KAFKA_TEMPLATE = re.compile(
    r"\b(\w*[Kk]afka\w*)\.send\s*\(\s*"
    + r'("((?:\\.|[^"\\])*)"|([A-Za-z_]\w*))'
)
_JAVA_RABBIT_PUBLISH = re.compile(
    r"\.basicPublish\s*\(\s*"
    + r'("((?:\\.|[^"\\])*)"|([A-Za-z_]\w*))\s*,\s*'
    + r'("((?:\\.|[^"\\])*)"|([A-Za-z_]\w*))'
)
_JAVA_RABBIT_TEMPLATE = re.compile(
    r"\.(?:convertAndSend|convertAndSendWithConfirm)\s*\(\s*"
    + r'("((?:\\.|[^"\\])*)"|([A-Za-z_]\w*))'
    + r"(?:\s*,\s*(\"((?:\\.|[^\"\\])*)\"|([A-Za-z_]\w*)))?"
)
_JAVA_ROCKET_MESSAGE = re.compile(
    r"new\s+Message\s*\(\s*"
    + r'("((?:\\.|[^"\\])*)"|([A-Za-z_]\w*))'
)
_JAVA_ROCKET_SEND = re.compile(
    r"\.(?:syncSend|asyncSend|sendOneWay|convertAndSend)\s*\(\s*"
    + r'("((?:\\.|[^"\\])*)"|([A-Za-z_]\w*))'
)
_PY_KAFKA_SEND = re.compile(
    r"\b(?P<producer>\w*[Pp]roducer\w*)\.send\s*\(\s*"
    r"(?P<topic>\"(?P<topic_dq>[^\"]*)\"|'(?P<topic_sq>[^']*)'"
    r"|(?P<topic_id>[A-Za-z_]\w*))"
)
_PY_PIKA_PUBLISH = re.compile(
    r"\.basic_publish\s*\(\s*(?:exchange\s*=\s*)?"
    r"(?P<exchange>\"(?P<exchange_dq>[^\"]*)\"|'(?P<exchange_sq>[^']*)'"
    r"|(?P<exchange_id>[A-Za-z_]\w*))"
    r"\s*,\s*(?:routing_key\s*=\s*)?"
    r"(?P<routing>\"(?P<routing_dq>[^\"]*)\"|'(?P<routing_sq>[^']*)'"
    r"|(?P<routing_id>[A-Za-z_]\w*))"
)
_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


@dataclass
class MqScanResult:
    producers: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: bool = True
    _warned: set[str] = field(default_factory=set, repr=False)


def _warn(result: MqScanResult, message: str) -> None:
    if message and message not in result._warned:
        result._warned.add(message)
        result.warnings.append(message)


@dataclass
class _Resolved:
    """一次 topic 取值结果；`source` 用于在画像里区分字面量与配置。"""

    value: str = ""
    source: str = "literal"
    config_key: str | None = None

    def __bool__(self) -> bool:
        return bool(self.value)


def _norm_key(key: str) -> str:
    """Spring relaxed binding：`mq.order-topic` / `mq.orderTopic` 等价。"""
    return key.strip().lower().replace("-", "").replace("_", "")


def _env_name(key: str) -> str:
    return key.strip().upper().replace(".", "_").replace("-", "_")


def _kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _strip_python_comment(text: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#":
            return text[:index]
    return text


class _Config:
    """只读 Spring 配置与 `.env`；不做运行期环境注入。"""

    def __init__(
        self,
        entries: dict[str, list[tuple[str, str]]],
        env: dict[str, tuple[str, str]],
        result: MqScanResult,
    ) -> None:
        self.entries = entries
        self.env = env
        self.result = result

    def lookup(
        self, key: str, location: str, depth: int = 0
    ) -> tuple[str | None, str | None]:
        entries = self.entries.get(_norm_key(key)) or []
        distinct = {value for _, value in entries}
        if len(distinct) > 1:
            _warn(self.result, f"mq_topic_config_ambiguous:{location}:{key}")
            return None, f"mq_topic_config_ambiguous:{location}:{key}"
        if entries:
            source, raw = entries[0]
            value, error = self.resolve_text(raw, location, depth + 1)
            if error is not None:
                return None, error
            return value, None
        env_name = _env_name(key)
        if env_name in self.env:
            return self.env[env_name][1], None
        return None, None

    def resolve_text(
        self, raw: object, location: str, depth: int = 0
    ) -> tuple[str | None, str | None]:
        text = "" if raw is None else str(raw)
        if "#{" in text:
            _warn(self.result, f"mq_topic_config_unsupported:{location}:spel")
            return None, f"mq_topic_config_unsupported:{location}:spel"
        while "${" in text:
            if depth > MAX_PLACEHOLDER_DEPTH:
                _warn(self.result, f"mq_topic_config_unsupported:{location}:too_deep")
                return None, f"mq_topic_config_unsupported:{location}:too_deep"
            match = _PLACEHOLDER.search(text)
            if match is None:
                break
            inner = match.group(1)
            key, has_default, default = inner.partition(":")
            value, error = self.lookup(key, location, depth + 1)
            if error is not None:
                return None, error
            if value is None:
                if not has_default:
                    message = f"mq_topic_config_missing:{location}:{key}"
                    _warn(self.result, message)
                    return None, message
                value = default
            text = text[: match.start()] + value + text[match.end():]
            depth += 1
        return text, None


def scan_mq_producers(root: str | Path) -> MqScanResult:
    project = Path(root).resolve()
    result = MqScanResult()
    if not project.is_dir():
        return MqScanResult(producers=[], warnings=["mq_project_root_missing"], checked=False)
    config = _load_config(project, result)
    seen: set[tuple[str, str]] = set()
    sources: list[tuple[str, str, str]] = []
    for path, language in _walk(project, result):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources.append((str(path.relative_to(project)), language, text))
    # `@ConfigurationProperties` 字段常与调用点不在同一个文件，先做全局索引。
    global_bindings: dict[str, _Resolved] = {}
    for relative, language, text in sources:
        if language != "java":
            continue
        for name, resolved in _java_config_property_bindings(
            relative, text, result, config
        ).items():
            if name not in global_bindings:
                global_bindings[name] = resolved
                continue
            existing = global_bindings[name]
            if (existing.value, existing.config_key) == (
                resolved.value, resolved.config_key
            ):
                continue
            _warn(result, f"mq_topic_config_ambiguous:binding:{name}")
            global_bindings[name] = _Resolved()
    for relative, language, text in sources:
        if language == "java":
            _scan_java(relative, text, result, seen, config, global_bindings)
        elif language == "python":
            _scan_python(relative, text, result, seen, config)
        if len(result.producers) >= MAX_PRODUCERS:
            _warn(result, "mq_producers_truncated")
            break
    result.producers.sort(
        key=lambda item: (item["client"], item["topic"], item["file"], item["line"])
    )
    return result


def _walk(project: Path, result: MqScanResult) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRS and not name.startswith(".")
        )
        for filename in sorted(filenames):
            suffix = Path(filename).suffix.lower()
            language = {".java": "java", ".py": "python"}.get(suffix)
            if language is None:
                continue
            path = Path(dirpath) / filename
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(project):
                    continue
                size = resolved.stat().st_size
            except OSError:
                continue
            if _looks_like_test(resolved.relative_to(project)):
                continue
            if size > MAX_FILE_BYTES:
                _warn(
                    result,
                    f"mq_source_skipped_large:{resolved.relative_to(project)}",
                )
                continue
            if total + size > MAX_TOTAL_BYTES:
                _warn(result, "mq_source_scan_truncated_total_bytes")
                return files
            total += size
            files.append((resolved, language))
            if len(files) >= MAX_FILES:
                _warn(result, "mq_source_scan_truncated_file_count")
                return files
    return files


def _looks_like_test(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    return any(
        part in {"test", "tests", "testing", "__tests__"}
        for part in parts[:-1]
    ) or relative.name.endswith(("Test.java", "_test.py", "test_.py"))


def _find_config_files(project: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRS and not name.startswith(".")
        )
        for filename in sorted(filenames):
            lower = filename.lower()
            if lower in {"application.properties", "application.yml", "application.yaml"} or (
                lower.startswith("application-")
                and lower.endswith((".properties", ".yml", ".yaml"))
            ):
                path = Path(dirpath) / filename
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > MAX_FILE_BYTES:
                    continue
                found.append(path)
                if len(found) >= MAX_CONFIG_FILES:
                    return sorted(found)
    return sorted(found)


def _load_config(project: Path, result: MqScanResult) -> _Config:
    entries: dict[str, list[tuple[str, str]]] = {}
    for path in _find_config_files(project):
        relative = str(path.relative_to(project))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            items = _parse_config(path.suffix.lower(), text)
        except (OSError, yaml.YAMLError, ValueError):
            _warn(result, f"mq_config_unreadable:{relative}")
            continue
        for key, value in items:
            if not key or value is None:
                continue
            entries.setdefault(_norm_key(key), []).append((relative, str(value)))
    env: dict[str, tuple[str, str]] = {}
    dotenv = project / ".env"
    if dotenv.is_file():
        try:
            env_text = dotenv.read_text(encoding="utf-8", errors="replace")
        except OSError:
            env_text = ""
        for name, value in _parse_properties(env_text):
            env[name] = (".env", value)
    return _Config(entries, env, result)


def _parse_config(suffix: str, text: str) -> list[tuple[str, object]]:
    if suffix == ".properties":
        return list(_parse_properties(text))
    data = yaml.safe_load(text)
    return _flatten_yaml(data)


def _parse_properties(text: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            items.append((key, value))
    return items


def _flatten_yaml(data: object, prefix: str = "") -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    if not isinstance(data, dict):
        return items
    for raw_key, value in data.items():
        key = f"{prefix}.{raw_key}" if prefix else str(raw_key)
        if isinstance(value, dict):
            items.extend(_flatten_yaml(value, key))
        elif isinstance(value, (str, int, float, bool)):
            items.append((key, value))
        # 列表、null 等不是合法的 topic 值，跳过，由缺失告警兜底。
    return items


def _scan_java(
    relative: str,
    text: str,
    result: MqScanResult,
    seen: set[tuple[str, str]],
    config: _Config,
    global_bindings: dict[str, _Resolved],
) -> None:
    constants = _java_constants(relative, text, result, config)
    bindings = {
        **global_bindings,
        **_java_value_bindings(relative, text, result, config),
    }
    context = _JavaContext(relative, result, constants, bindings, config)
    for match in _JAVA_KAFKA_RECORD.finditer(text):
        resolved = _java_value(match, context, 2, 3, "kafka_producer_record", text)
        _add(
            result, seen, "kafka", resolved,
            relative, _line(text, match.start()), "kafka_producer_record",
        )
    for match in _JAVA_KAFKA_TEMPLATE.finditer(text):
        resolved = _java_value(match, context, 3, 4, "kafka_template", text)
        _add(
            result, seen, "kafka", resolved,
            relative, _line(text, match.start()), "kafka_template",
        )
    for match in _JAVA_RABBIT_PUBLISH.finditer(text):
        exchange = _java_value(match, context, 2, 3, "rabbit_basic_publish", text)
        routing = _java_value(match, context, 5, 6, "rabbit_basic_publish", text)
        _add(
            result, seen, "rabbitmq", _joined(exchange, routing),
            relative, _line(text, match.start()), "rabbit_basic_publish",
        )
    for match in _JAVA_RABBIT_TEMPLATE.finditer(text):
        exchange = _java_value(match, context, 2, 3, "rabbit_template", text)
        routing = (
            _java_value(match, context, 5, 6, "rabbit_template", text)
            if match.group(4)
            else _Resolved()
        )
        if routing:
            topic = _joined(exchange, routing)
        elif exchange:
            topic = _Resolved(
                f"/{exchange.value}", exchange.source, exchange.config_key
            )
        else:
            topic = _Resolved()
        _add(
            result, seen, "rabbitmq", topic,
            relative, _line(text, match.start()), "rabbit_template",
        )
    for match in _JAVA_ROCKET_MESSAGE.finditer(text):
        resolved = _java_value(match, context, 2, 3, "rocket_message", text)
        _add(
            result, seen, "rocketmq", resolved,
            relative, _line(text, match.start()), "rocket_message",
        )
    for match in _JAVA_ROCKET_SEND.finditer(text):
        raw = _java_value(match, context, 2, 3, "rocket_template", text)
        topic = _Resolved(
            raw.value.partition(":")[0], raw.source, raw.config_key
        )
        _add(
            result, seen, "rocketmq", topic,
            relative, _line(text, match.start()), "rocket_template",
        )


@dataclass
class _JavaContext:
    relative: str
    result: MqScanResult
    constants: dict[str, str]
    bindings: dict[str, _Resolved]
    config: _Config


def _java_constants(
    relative: str, text: str, result: MqScanResult, config: _Config
) -> dict[str, str]:
    constants: dict[str, str] = {}
    for match in _JAVA_CONST.finditer(text):
        name = match.group(1)
        literal = match.group(2)
        if "${" in literal:
            location = f"{relative}:{_line(text, match.start())}"
            value, error = config.resolve_text(literal, location)
            if error is not None:
                continue
            constants[name] = value or ""
        else:
            constants[name] = literal
    return constants


def _placeholder_key(raw: str) -> str | None:
    match = _PLACEHOLDER.search(raw)
    if match is None:
        return None
    return match.group(1).partition(":")[0]


def _java_value_bindings(
    relative: str, text: str, result: MqScanResult, config: _Config
) -> dict[str, _Resolved]:
    bindings: dict[str, _Resolved] = {}
    for match in _JAVA_VALUE_ANNOTATION.finditer(text):
        target = _JAVA_VALUE_TARGET.search(text, match.end(), match.end() + 240)
        if target is None:
            continue
        name = target.group(1)
        raw = match.group(1)
        if "${" not in raw:
            bindings[name] = _Resolved(raw, "literal")
            continue
        location = f"{relative}:{_line(text, match.start())}:@Value"
        value, error = config.resolve_text(raw, location)
        if error is None and value is not None:
            bindings[name] = _Resolved(value, "config", _placeholder_key(raw))
        else:
            bindings[name] = _Resolved()
    return bindings


def _java_config_property_bindings(
    relative: str, text: str, result: MqScanResult, config: _Config
) -> dict[str, _Resolved]:
    bindings: dict[str, _Resolved] = {}
    for match in _JAVA_CONFIG_PROPERTIES.finditer(text):
        prefix = match.group(1)
        body, body_start = _class_body(text, match.end())
        if body is None:
            continue
        for field_match in _JAVA_CONFIG_FIELD.finditer(body):
            name = field_match.group(1)
            key = f"{prefix}.{_kebab(name)}"
            location = (
                f"{relative}:{_line(text, body_start + field_match.start())}:"
                f"@ConfigurationProperties"
            )
            value, error = config.lookup(key, location)
            if error is None and value is not None:
                bindings[name] = _Resolved(value, "config", key)
                continue
            literal = field_match.group(2)
            if error is None and literal is not None:
                bindings[name] = _Resolved(literal, "literal")
                continue
            bindings[name] = _Resolved()
    return bindings


def _class_body(text: str, start: int) -> tuple[str | None, int]:
    match = _JAVA_CLASS.search(text, start)
    if match is None:
        return None, start
    depth = 0
    for index in range(match.end() - 1, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[match.end():index], match.end()
    return None, start


def _java_value(
    match: re.Match,
    context: _JavaContext,
    literal: int,
    identifier: int,
    kind: str,
    text: str,
) -> _Resolved:
    value = match.group(literal)
    if value is not None:
        if "${" in value:
            location = f"{context.relative}:{_line(text, match.start())}:{kind}"
            resolved, error = context.config.resolve_text(value, location)
            if error is not None:
                return _Resolved()
            return _Resolved(
                resolved or "", "config", _placeholder_key(value)
            )
        return _Resolved(value)
    name = match.group(identifier) or ""
    binding = _binding_for(name, match, context, text)
    if binding is not None:
        return binding
    if name in context.constants:
        return _Resolved(context.constants[name], "constant")
    _warn(
        context.result,
        f"mq_topic_unresolved:{context.relative}:"
        f"{_line(text, match.start())}:{kind}:{name}",
    )
    return _Resolved()


def _binding_for(
    name: str, match: re.Match, context: _JavaContext, text: str
) -> _Resolved | None:
    """解析 `@Value` 字段、`properties.payTopic` 与 `properties.getPayTopic()`。

    返回 `_Resolved` 表示命中（空值表示配置缺失、已告警），None 表示未命中。
    """
    if name in context.bindings:
        return context.bindings[name]
    member = re.match(r"\.\s*([A-Za-z_]\w*)", text[match.end(): match.end() + 120])
    if member is None:
        return None
    member_name = member.group(1)
    if member_name.startswith(("get", "is")) and len(member_name) > 3:
        member_name = member_name[3].lower() + member_name[4:]
    if member_name in context.bindings:
        return context.bindings[member_name]
    return None


def _joined(exchange: _Resolved, routing: _Resolved) -> _Resolved:
    if not exchange and not routing:
        return _Resolved()
    source = exchange if exchange.source != "literal" else routing
    return _Resolved(
        f"{exchange.value}/{routing.value}",
        source.source,
        source.config_key,
    )


def _scan_python(
    relative: str,
    text: str,
    result: MqScanResult,
    seen: set[tuple[str, str]],
    config: _Config,
) -> None:
    values, unresolved = _python_values(relative, text, config)
    for match in _PY_KAFKA_SEND.finditer(text):
        topic = _py_named(
            match, values, unresolved, "topic", relative, text, "kafka_send", result
        )
        _add(
            result, seen, "kafka", topic,
            relative, _line(text, match.start()), "kafka_send",
        )
    for match in _PY_PIKA_PUBLISH.finditer(text):
        exchange = _py_named(
            match, values, unresolved, "exchange", relative, text,
            "pika_basic_publish", result,
        )
        routing = _py_named(
            match, values, unresolved, "routing", relative, text,
            "pika_basic_publish", result,
        )
        _add(
            result, seen, "rabbitmq", _joined(exchange, routing),
            relative, _line(text, match.start()), "pika_basic_publish",
        )


def _python_values(
    relative: str, text: str, config: _Config
) -> tuple[dict[str, _Resolved], dict[str, tuple[str, str]]]:
    values: dict[str, _Resolved] = {}
    unresolved: dict[str, tuple[str, str]] = {}
    for match in _PY_ASSIGN.finditer(text):
        name = match.group(1)
        rhs = _strip_python_comment(match.group(2)).strip()
        literal = _PY_LITERAL.match(rhs)
        if literal is not None:
            values[name] = _Resolved(literal.group(1), "constant")
            continue
        env = _PY_GETENV.fullmatch(rhs)
        if env is not None:
            env_name = env.group(1)
            default = env.group(2)
            if default is not None:
                values[name] = _Resolved(default, "default", env_name)
                continue
            if env_name in config.env:
                values[name] = _Resolved(
                    config.env[env_name][1], "env", env_name
                )
                continue
            unresolved[name] = ("env", env_name)
            continue
        index = _PY_ENVIRON_INDEX.fullmatch(rhs)
        if index is not None:
            env_name = index.group(1)
            if env_name in config.env:
                values[name] = _Resolved(
                    config.env[env_name][1], "env", env_name
                )
            else:
                unresolved[name] = ("env", env_name)
            continue
        unresolved[name] = ("dynamic", rhs[:40])
    return values, unresolved


def _py_named(
    match: re.Match,
    values: dict[str, _Resolved],
    unresolved: dict[str, tuple[str, str]],
    prefix: str,
    relative: str,
    text: str,
    kind: str,
    result: MqScanResult,
) -> _Resolved:
    for suffix in ("_dq", "_sq"):
        value = match.group(f"{prefix}{suffix}")
        if value is not None:
            return _Resolved(value)
    name = match.group(f"{prefix}_id") or ""
    if name in values:
        return values[name]
    line = _line(text, match.start())
    if name in unresolved:
        reason, detail = unresolved[name]
        if reason == "env":
            _warn(
                result,
                f"mq_topic_config_missing:{relative}:{line}:{kind}:{detail}",
            )
        else:
            _warn(
                result,
                f"mq_topic_unresolved:{relative}:{line}:{kind}:{name}",
            )
        return _Resolved()
    _warn(
        result,
        f"mq_topic_unresolved:{relative}:{line}:{kind}:{name}",
    )
    return _Resolved()


def _add(
    result: MqScanResult,
    seen: set[tuple[str, str]],
    client: str,
    resolved: _Resolved,
    file: str,
    line: int,
    source: str,
) -> None:
    topic = (resolved.value or "").strip()
    if not topic:
        return
    key = (client, topic)
    if key in seen:
        return
    seen.add(key)
    item = {
        "dep_id": f"mq:{client}:{topic}",
        "client": client,
        "topic": topic,
        "file": file,
        "line": line,
        "source": source,
    }
    if resolved.source != "literal":
        item["topic_source"] = resolved.source
        if resolved.config_key:
            item["config_key"] = resolved.config_key
    result.producers.append(item)


__all__ = ["MqScanResult", "scan_mq_producers"]
