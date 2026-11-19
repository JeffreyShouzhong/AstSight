"""Python SUT MQ 生产者拦截 e2e：kafka-python / pika，不连 broker。

默认跳过。启用条件：

- `ASTSIGHT_MQ_E2E=1`
- 当前 Python 解释器可导入 `kafka` 与 `pika`

`sitecustomize` 注入方式与 `agent run` 的 Python SUT 相同：把
`engine/astsight/mq/inject` 加到 `PYTHONPATH`，设置 `ASTSIGHT_MQ_CAPTURE=1`。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from astsight.mock import MockHttpServer

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_MQ_E2E") != "1"
    or importlib.util.find_spec("kafka") is None
    or importlib.util.find_spec("pika") is None,
    reason="Python MQ e2e 默认跳过：设置 ASTSIGHT_MQ_E2E=1 且安装 kafka-python/pika",
)

ROOT = Path(__file__).resolve().parents[3]
INJECT = ROOT / "engine" / "astsight" / "mq" / "inject"
TOKEN = "py-mq-token"
SUT_ID = "py-mq-sut"

_SCRIPT = r'''
import json
import sys

client, topic, body = sys.argv[1], sys.argv[2], sys.argv[3]
if client == "kafka":
    from kafka import KafkaProducer
    producer = KafkaProducer(
        bootstrap_servers="127.0.0.1:1",
        api_version=(2, 0, 0),
        max_block_ms=1000,
    )
    future = producer.send(
        topic,
        value=body.encode("utf-8"),
        key=b"K-1",
        headers=[("trace", b"t1")],
    )
    print("result", future.result(timeout=5))
else:
    import pika
    channel = object.__new__(pika.channel.Channel)
    properties = pika.BasicProperties(headers={"trace": "t1"})
    result = channel.basic_publish(
        "orders-exchange",
        topic,
        body.encode("utf-8"),
        properties,
    )
    print("result", result)
print("astsight-python-mq:ok")
'''


def _start_mock(client: str, topic: str, tmp_path: Path) -> MockHttpServer:
    server = MockHttpServer(
        sut_id=SUT_ID,
        token=TOKEN,
        history_path=tmp_path / f"{client}.jsonl",
    )
    server.add_mq_dep(f"mq:{client}:{topic}", topic=topic, client=client)
    server.start()
    return server


def _create_session(server: MockHttpServer) -> None:
    response = httpx.post(
        f"{server.admin_endpoint}/_admin/sessions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "session_id": "session-" + uuid.uuid4().hex,
            "namespace": SUT_ID,
            "case_id": "TC-PY-MQ",
            "bindings": [],
        },
        timeout=5,
    )
    assert response.status_code == 200, response.text


def _messages(server: MockHttpServer) -> list[dict]:
    response = httpx.get(
        f"{server.admin_endpoint}/_admin/mq/messages",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=5,
    )
    assert response.status_code == 200, response.text
    return response.json()["messages"]


@pytest.mark.parametrize(
    ("client", "topic", "expected_topic", "expected_key"),
    [
        ("kafka", "orders-topic", "orders-topic", "K-1"),
        (
            "rabbitmq",
            "order.created",
            "orders-exchange/order.created",
            "order.created",
        ),
    ],
)
def test_python_producer_message_is_intercepted(
    client: str,
    topic: str,
    expected_topic: str,
    expected_key: str,
    tmp_path: Path,
) -> None:
    body = '{"order_id":"O-1001","status":"paid"}'
    script = tmp_path / "sut.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    server = _start_mock(client, expected_topic, tmp_path)
    try:
        _create_session(server)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(INJECT) + os.pathsep + env.get("PYTHONPATH", "")
        env["ASTSIGHT_MQ_CAPTURE"] = "1"
        env["ASTSIGHT_MQ_ADMIN_URL"] = server.admin_endpoint
        env["ASTSIGHT_MQ_TOKEN"] = TOKEN
        env["ASTSIGHT_MQ_SUT_ID"] = SUT_ID
        completed = subprocess.run(
            [sys.executable, str(script), client, topic, body],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        assert completed.returncode == 0, (
            completed.stdout + completed.stderr
        )
        assert "astsight-python-mq:ok" in completed.stdout

        messages = _messages(server)
        assert len(messages) == 1, messages
        message = messages[0]
        assert message["topic"] == expected_topic
        assert message["client"] == client
        assert message["key"] == expected_key
        assert message["headers"].get("trace") == "t1"
        assert message["body"] == {
            "order_id": "O-1001",
            "status": "paid",
        }
        calls = server.evidence.records
        assert len(calls) == 1
        assert calls[0].transport == "mq"
        assert calls[0].mode == "producer_intercept"
    finally:
        server.stop()
