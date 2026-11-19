"""Java agent MQ 生产者拦截 e2e：真实客户端构造消息，不连 broker。

默认跳过。启用条件：

- `ASTSIGHT_MQ_E2E=1`
- `java/astsight-agent/target/astsight-agent.jar` 已构建
- `java/samples/mq-producer/target/mq-producer-sample.jar` 已构建
- `JAVA_HOME` 或 `ASTSIGHT_JAVA_AGENT_E2E_JAVA` 指向 JDK

验证 KafkaProducer、RabbitMQ ChannelN、RocketMQ DefaultMQProducer 的消息
被 Agent 截获：SUT 不连 broker，Mock 服务收到 topic/key/headers/body。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from astsight.mock import MockHttpServer

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_MQ_E2E") != "1",
    reason="Java MQ e2e 默认跳过：设置 ASTSIGHT_MQ_E2E=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "mq-e2e-token"
SUT_ID = "mq-e2e-sut"


def _executable(path: Path) -> Path | None:
    if path.is_file():
        return path
    if os.name == "nt":
        exe = Path(str(path) + ".exe")
        if exe.is_file():
            return exe
    return None


def _java() -> Path:
    configured = os.environ.get("ASTSIGHT_JAVA_AGENT_E2E_JAVA", "").strip()
    if configured:
        candidate = Path(configured)
        tool = candidate / "bin" / "java" if candidate.is_dir() else candidate
        found = _executable(tool)
        if found is not None:
            return found
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        found = _executable(Path(home) / "bin" / "java")
        if found is not None:
            return found
    found = shutil.which("java")
    if found:
        return Path(found)
    pytest.skip("未找到 java；设置 JAVA_HOME 或 ASTSIGHT_JAVA_AGENT_E2E_JAVA")


def _agent_jar() -> Path:
    path = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not path.is_file():
        pytest.skip("agent jar 未构建：java/mvn -q -DskipTests package")
    return path


def _sample_jar() -> Path:
    path = (
        ROOT
        / "java"
        / "samples"
        / "mq-producer"
        / "target"
        / "mq-producer-sample.jar"
    )
    if not path.is_file():
        pytest.skip(
            "MQ 样例 jar 未构建："
            "mvn -q -f java/samples/mq-producer/pom.xml package"
        )
    return path


def _start_mock(client: str, topic: str, tmp_path: Path) -> MockHttpServer:
    server = MockHttpServer(
        sut_id=SUT_ID,
        token=TOKEN,
        history_path=tmp_path / f"{client}.jsonl",
    )
    server.add_mq_dep(
        f"mq:{client}:{topic}", topic=topic, client=client
    )
    server.start()
    return server


def _create_session(server: MockHttpServer) -> None:
    response = httpx.post(
        f"{server.admin_endpoint}/_admin/sessions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "session_id": "session-" + uuid.uuid4().hex,
            "namespace": SUT_ID,
            "case_id": "TC-MQ-E2E",
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
    ("client", "topic", "expected_key", "expected_headers"),
    [
        ("kafka", "orders-topic", "K-1", {}),
        (
            "rabbitmq",
            "orders-exchange/order.created",
            "order.created",
            {"trace": "t1"},
        ),
        ("rocketmq", "orders-rocket-topic", "K-1", {"trace": "t1"}),
    ],
)
def test_producer_message_is_intercepted(
    client: str,
    topic: str,
    expected_key: str,
    expected_headers: dict,
    tmp_path: Path,
) -> None:
    java = _java()
    agent = _agent_jar()
    sample = _sample_jar()
    body = '{"order_id":"O-1001","status":"paid"}'
    server = _start_mock(client, topic, tmp_path)
    try:
        _create_session(server)
        env = os.environ.copy()
        env["ASTSIGHT_AGENT_TOKEN"] = TOKEN
        completed = subprocess.run(
            [
                str(java),
                f"-javaagent:{agent}=admin={server.admin_endpoint},"
                "token_env=ASTSIGHT_AGENT_TOKEN,"
                f"sut_id={SUT_ID}",
                "-jar",
                str(sample),
                client,
                topic,
                body,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
        assert completed.returncode == 0, (
            completed.stdout + completed.stderr
        )
        assert "astsight-mq-sample:ok" in completed.stdout

        messages = _messages(server)
        assert len(messages) == 1, messages
        message = messages[0]
        assert message["topic"] == topic
        assert message["client"] == client
        assert message["key"] == expected_key
        assert all(
            message["headers"].get(name) == value
            for name, value in expected_headers.items()
        ), message["headers"]
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
