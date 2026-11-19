"""MQ 生产者拦截版本矩阵：真实客户端版本 + Java agent，不连 broker。

默认跳过。启用条件：

- `ASTSIGHT_MQ_MATRIX=1`
- agent jar 已构建
- JAVA_HOME 指向 JDK 17+
- PATH 或 `ASTSIGHT_MVN` 能找到 Maven

每个版本用 Maven 解析一次 classpath，javac 编译样例后运行；不构建 76MB
shade jar，避免矩阵过慢。
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
    os.environ.get("ASTSIGHT_MQ_MATRIX") != "1",
    reason="MQ 版本矩阵默认跳过：设置 ASTSIGHT_MQ_MATRIX=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "java" / "samples" / "mq-producer"
AGENT = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
TOKEN = "mq-matrix-token"
SUT_ID = "mq-matrix-sut"

MATRIX = [
    ("kafka", "2.8.2", "kafka"),
    ("kafka", "3.7.0", "kafka"),
    ("rabbitmq", "4.9.0", "rabbitmq"),
    ("rabbitmq", "5.21.0", "rabbitmq"),
    ("rocketmq", "4.9.7", "rocketmq"),
    ("rocketmq", "5.3.1", "rocketmq"),
]


def _executable(path: Path) -> Path | None:
    if os.name == "nt" and not path.suffix:
        for suffix in (".cmd", ".exe", ".bat"):
            candidate = Path(str(path) + suffix)
            if candidate.is_file():
                return candidate
    if path.is_file():
        return path
    if os.name == "nt":
        for suffix in (".exe", ".cmd", ".bat"):
            candidate = Path(str(path) + suffix)
            if candidate.is_file():
                return candidate
    return None


def _java_tool(name: str) -> Path:
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        found = _executable(Path(home) / "bin" / name)
        if found is not None:
            return found
    found = shutil.which(name)
    if found:
        return Path(found)
    pytest.skip(f"未找到 {name}；设置 JAVA_HOME")


def _mvn() -> Path:
    configured = os.environ.get("ASTSIGHT_MVN", "").strip()
    if configured:
        found = _executable(Path(configured))
        if found is not None:
            return found
    found = shutil.which("mvn")
    if found:
        return Path(found)
    pytest.skip("未找到 mvn；设置 ASTSIGHT_MVN")


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> None:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    assert completed.returncode == 0, (
        " ".join(command) + "\n" + completed.stdout + completed.stderr
    )


def _prepare(client: str, version: str, work: Path) -> tuple[Path, str]:
    mvn = _mvn()
    javac = _java_tool("javac")
    cp_file = work / f"cp-{client}-{version}.txt"
    property_name = {
        "kafka": "kafka.version",
        "rabbitmq": "rabbit.version",
        "rocketmq": "rocketmq.version",
    }[client]
    _run(
        [
            str(mvn),
            "-q",
            "-f",
            str(SAMPLE / "pom.xml"),
            "dependency:build-classpath",
            f"-Dmdep.outputFile={cp_file}",
            f"-D{property_name}={version}",
        ],
        cwd=SAMPLE,
        timeout=600,
    )
    classpath = cp_file.read_text(encoding="utf-8").strip()
    classes = work / "classes"
    classes.mkdir()
    _run(
        [
            str(javac),
            "-encoding",
            "UTF-8",
            "-cp",
            classpath,
            "-d",
            str(classes),
            str(
                SAMPLE
                / "src/main/java/io/astsight/sample/mq/MqProducerSample.java"
            ),
        ],
        cwd=SAMPLE,
        timeout=120,
    )
    return classes, classpath


def _start_mock(client: str, topic: str, tmp_path: Path) -> MockHttpServer:
    server = MockHttpServer(
        sut_id=SUT_ID,
        token=TOKEN,
        history_path=tmp_path / f"{client}-{uuid.uuid4().hex}.jsonl",
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
            "case_id": "TC-MQ-MATRIX",
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


@pytest.mark.parametrize(("client", "version", "expected_client"), MATRIX)
def test_mq_client_version_intercepts(
    client: str, version: str, expected_client: str, tmp_path: Path
) -> None:
    if not AGENT.is_file():
        pytest.skip("agent jar 未构建")
    java = _java_tool("java")
    classes, classpath = _prepare(client, version, tmp_path)
    topic = f"orders-{client}-{version.replace('.', '-')}"
    if client == "rabbitmq":
        topic = f"orders-exchange/{topic}"
    body = '{"order_id":"O-1001"}'
    server = _start_mock(expected_client, topic, tmp_path)
    try:
        _create_session(server)
        env = os.environ.copy()
        env["ASTSIGHT_AGENT_TOKEN"] = TOKEN
        _run(
            [
                str(java),
                f"-javaagent:{AGENT}=admin={server.admin_endpoint},"
                "token_env=ASTSIGHT_AGENT_TOKEN,"
                f"sut_id={SUT_ID}",
                "-cp",
                str(classes) + os.pathsep + classpath,
                "io.astsight.sample.mq.MqProducerSample",
                expected_client,
                topic,
                body,
            ],
            cwd=SAMPLE,
            timeout=120,
            env=env,
        )
        messages = _messages(server)
        assert len(messages) == 1, (client, version, messages)
        assert messages[0]["topic"] == topic
        assert messages[0]["body"] == {"order_id": "O-1001"}
    finally:
        server.stop()
