"""kafka-python / pika 生产者侧拦截。

命中计划内 topic 时不真正发送，把消息 POST 给 Mock 服务的
`/_admin/mq/publish`；计划外继续原调用。不连接任何 broker。
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import sys
import threading
import urllib.request
from concurrent.futures import Future
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from typing import Any

_ENABLED = os.environ.get("ASTSIGHT_MQ_CAPTURE") == "1"
_ADMIN = os.environ.get("ASTSIGHT_MQ_ADMIN_URL", "").rstrip("/")
_TOKEN = os.environ.get("ASTSIGHT_MQ_TOKEN", "")
_SUT_ID = os.environ.get("ASTSIGHT_MQ_SUT_ID", "")
_TIMEOUT = 2.0
_LOCAL = threading.local()
_PATCHED = False


def install() -> None:
    global _PATCHED
    if _PATCHED or not _ENABLED or not _ADMIN:
        return
    _PATCHED = True
    sys.meta_path.insert(0, _Finder())
    _send_heartbeat()


def _send_heartbeat() -> None:
    """让 Mock 门禁知道 Python 生产者拦截已安装。"""
    clients = []
    for name in ("kafka", "pika"):
        try:
            if importlib.util.find_spec(name):
                clients.append("kafka" if name == "kafka" else "rabbitmq")
        except Exception:
            continue
    payload = {
        "sut_id": _SUT_ID,
        "agent_version": "python-mq-capture",
        "pid": os.getpid(),
        "jvm_start_time_ms": 0,
        "http_adapters": [],
        "mq": {
            "installed": True,
            "clients": sorted(set(clients)),
            "published": 0,
            "suppressed": 0,
            "errors": 0,
        },
    }
    request = urllib.request.Request(
        f"{_ADMIN}/_admin/agent/heartbeat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT):
            pass
    except Exception:
        pass


def _publish(
    client: str,
    topic: str,
    key: Any,
    headers: dict[str, str],
    body: Any,
) -> bool:
    if not _ENABLED or not _ADMIN:
        return False
    payload = {
        "sut_id": _SUT_ID,
        "client": client,
        "topic": topic or "",
        "key": _jsonable(key),
        "headers": {
            str(name): _text(value) for name, value in (headers or {}).items()
        },
        "body": _jsonable(body),
    }
    request = urllib.request.Request(
        f"{_ADMIN}/_admin/mq/publish",
        data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        return bool(result.get("handled"))
    except Exception:
        return False


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="replace")
        parsed = _try_json(text)
        return parsed if parsed is not None else text
    if isinstance(value, str):
        parsed = _try_json(value)
        return parsed if parsed is not None else value
    if isinstance(value, (dict, list)):
        return value
    return str(value)


def _try_json(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _kafka_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not headers:
        return result
    try:
        for name, value in headers:
            result[str(name)] = _text(value)
    except (TypeError, ValueError):
        return result
    return result


class _Finder(MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: Any = None,
    ) -> ModuleSpec | None:
        patch = _targets().get(fullname)
        if patch is None:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is None or spec.loader is None:
                continue
            if hasattr(spec.loader, "exec_module"):
                spec.loader = _PatchLoader(spec.loader, patch)
            return spec
        return None


def _targets() -> dict[str, Any]:
    return {
        "kafka.producer.kafka": _patch_kafka,
        "pika.channel": _patch_pika_channel,
        "pika.adapters.blocking_connection": _patch_pika_blocking,
    }


class _PatchLoader(Loader):
    def __init__(self, loader: Any, patch: Any) -> None:
        self.loader = loader
        self.patch = patch

    def create_module(self, spec: ModuleSpec) -> Any:
        create = getattr(self.loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module: Any) -> None:
        self.loader.exec_module(module)
        try:
            self.patch(module)
        except Exception:
            pass


def _patch_kafka(module: Any) -> None:
    producer = getattr(module, "KafkaProducer", None)
    if producer is None or getattr(producer.send, "_astsight_mq", False):
        return
    original = producer.send

    @functools.wraps(original)
    def send(
        self: Any,
        topic: str,
        value: Any = None,
        key: Any = None,
        headers: Any = None,
        partition: Any = None,
        timestamp_ms: Any = None,
    ) -> Any:
        if _publish(
            "kafka",
            topic,
            key,
            _kafka_headers(headers),
            value,
        ):
            future: Future[Any] = Future()
            future.set_result(None)
            return future
        return original(
            self,
            topic,
            value=value,
            key=key,
            headers=headers,
            partition=partition,
            timestamp_ms=timestamp_ms,
        )

    send._astsight_mq = True
    producer.send = send


def _patch_pika_channel(module: Any) -> None:
    channel = getattr(module, "Channel", None)
    if channel is None:
        return
    _wrap_pika(channel, returns_true=False)


def _patch_pika_blocking(module: Any) -> None:
    channel = getattr(module, "BlockingChannel", None)
    if channel is None:
        return
    _wrap_pika(channel, returns_true=True)


def _wrap_pika(channel: Any, *, returns_true: bool) -> None:
    original = getattr(channel, "basic_publish", None)
    if original is None or getattr(original, "_astsight_mq", False):
        return

    @functools.wraps(original)
    def basic_publish(
        self: Any,
        exchange: str,
        routing_key: str,
        body: Any,
        properties: Any = None,
        mandatory: bool = False,
        immediate: bool = False,
    ) -> Any:
        if getattr(_LOCAL, "in_publish", False):
            return _call_pika(
                original,
                self,
                exchange,
                routing_key,
                body,
                properties,
                mandatory,
                immediate,
            )
        headers = getattr(properties, "headers", None) or {}
        if _publish(
            "rabbitmq",
            f"{exchange}/{routing_key}",
            routing_key,
            headers,
            body,
        ):
            return True if returns_true else None
        _LOCAL.in_publish = True
        try:
            return _call_pika(
                original,
                self,
                exchange,
                routing_key,
                body,
                properties,
                mandatory,
                immediate,
            )
        finally:
            _LOCAL.in_publish = False

    basic_publish._astsight_mq = True
    channel.basic_publish = basic_publish


def _call_pika(
    original: Any,
    self: Any,
    exchange: str,
    routing_key: str,
    body: Any,
    properties: Any,
    mandatory: bool,
    immediate: bool,
) -> Any:
    try:
        return original(
            self,
            exchange,
            routing_key,
            body,
            properties,
            mandatory,
            immediate,
        )
    except TypeError:
        return original(self, exchange, routing_key, body, properties, mandatory)
