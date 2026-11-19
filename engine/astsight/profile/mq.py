"""MQ 生产者调用点扫描：只读源码，识别 topic / exchange / routingKey。

只用于给画像和 A4 提供候选，不改变用例；识别不到常量时显式写 warning，
不猜动态值。当前覆盖 Java（Kafka/RabbitMQ/RocketMQ）与 Python（kafka-python/pika）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 2_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PRODUCERS = 200
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
_PY_STRING = r'["\']([^"\']*)["\']'
_PY_CONST = re.compile(
    r"^[ \t]*([A-Za-z_]\w*)\s*=\s*[\"']([^\"']*)[\"']", re.M
)
_JAVA_CONST = re.compile(
    r"(?:static\s+final|final\s+static)\s+String\s+(\w+)\s*=\s*" + _JAVA_STRING
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


@dataclass
class MqScanResult:
    producers: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: bool = True


def scan_mq_producers(root: str | Path) -> MqScanResult:
    project = Path(root).resolve()
    result = MqScanResult()
    if not project.is_dir():
        return MqScanResult(producers=[], warnings=["mq_project_root_missing"], checked=False)
    seen: set[tuple[str, str]] = set()
    files = _walk(project, result)
    for path, language in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = str(path.relative_to(project))
        if language == "java":
            _scan_java(relative, text, result, seen)
        elif language == "python":
            _scan_python(relative, text, result, seen)
        if len(result.producers) >= MAX_PRODUCERS:
            result.warnings.append("mq_producers_truncated")
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
                result.warnings.append(
                    f"mq_source_skipped_large:{resolved.relative_to(project)}"
                )
                continue
            if total + size > MAX_TOTAL_BYTES:
                result.warnings.append("mq_source_scan_truncated_total_bytes")
                return files
            total += size
            files.append((resolved, language))
            if len(files) >= MAX_FILES:
                result.warnings.append("mq_source_scan_truncated_file_count")
                return files
    return files


def _looks_like_test(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    return any(
        part in {"test", "tests", "testing", "__tests__"}
        for part in parts[:-1]
    ) or relative.name.endswith(("Test.java", "_test.py", "test_.py"))


def _scan_java(
    relative: str, text: str, result: MqScanResult, seen: set[tuple[str, str]]
) -> None:
    constants = {name: value for name, value in _JAVA_CONST.findall(text)}
    for match in _JAVA_KAFKA_RECORD.finditer(text):
        _add(
            result, seen, "kafka", _java_value(match, constants, 2, 3),
            relative, _line(text, match.start()), "kafka_producer_record",
        )
    for match in _JAVA_KAFKA_TEMPLATE.finditer(text):
        _add(
            result, seen, "kafka", _java_value(match, constants, 3, 4),
            relative, _line(text, match.start()), "kafka_template",
        )
    for match in _JAVA_RABBIT_PUBLISH.finditer(text):
        exchange = _java_value(match, constants, 2, 3)
        routing = _java_value(match, constants, 5, 6)
        _add(
            result, seen, "rabbitmq", f"{exchange}/{routing}",
            relative, _line(text, match.start()), "rabbit_basic_publish",
        )
    for match in _JAVA_RABBIT_TEMPLATE.finditer(text):
        exchange = _java_value(match, constants, 2, 3)
        routing = _java_value(match, constants, 5, 6) if match.group(4) else ""
        topic = f"{exchange}/{routing}" if routing else f"/{exchange}"
        _add(
            result, seen, "rabbitmq", topic,
            relative, _line(text, match.start()), "rabbit_template",
        )
    for match in _JAVA_ROCKET_MESSAGE.finditer(text):
        _add(
            result, seen, "rocketmq", _java_value(match, constants, 2, 3),
            relative, _line(text, match.start()), "rocket_message",
        )
    for match in _JAVA_ROCKET_SEND.finditer(text):
        raw = _java_value(match, constants, 2, 3)
        topic = raw.partition(":")[0]
        _add(
            result, seen, "rocketmq", topic,
            relative, _line(text, match.start()), "rocket_template",
        )
    for match in re.finditer(
        r"new\s+ProducerRecord\s*(?:<[^>]*>)?\s*\(\s*([A-Za-z_]\w*)",
        text,
    ):
        result.warnings.append(
            f"mq_topic_unresolved:{relative}:{_line(text, match.start())}:"
            f"{match.group(1)}"
        )


def _scan_python(
    relative: str, text: str, result: MqScanResult, seen: set[tuple[str, str]]
) -> None:
    constants = {name: value for name, value in _PY_CONST.findall(text)}
    for match in _PY_KAFKA_SEND.finditer(text):
        topic = _py_named(match, constants, "topic")
        _add(
            result, seen, "kafka", topic,
            relative, _line(text, match.start()), "kafka_send",
        )
    for match in _PY_PIKA_PUBLISH.finditer(text):
        exchange = _py_named(match, constants, "exchange")
        routing = _py_named(match, constants, "routing")
        _add(
            result, seen, "rabbitmq", f"{exchange}/{routing}",
            relative, _line(text, match.start()), "pika_basic_publish",
        )


def _add(
    result: MqScanResult,
    seen: set[tuple[str, str]],
    client: str,
    topic: str,
    file: str,
    line: int,
    source: str,
) -> None:
    topic = (topic or "").strip()
    if not topic:
        return
    key = (client, topic)
    if key in seen:
        return
    seen.add(key)
    result.producers.append(
        {
            "dep_id": f"mq:{client}:{topic}",
            "client": client,
            "topic": topic,
            "file": file,
            "line": line,
            "source": source,
        }
    )


def _java_value(
    match: re.Match, constants: dict[str, str], literal: int, identifier: int
) -> str:
    value = match.group(literal)
    if value is not None:
        return value
    return constants.get(match.group(identifier) or "", "")


def _py_named(
    match: re.Match, constants: dict[str, str], prefix: str
) -> str:
    for suffix in ("_dq", "_sq"):
        value = match.group(f"{prefix}{suffix}")
        if value is not None:
            return value
    return constants.get(match.group(f"{prefix}_id") or "", "")


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


__all__ = ["MqScanResult", "scan_mq_producers"]
