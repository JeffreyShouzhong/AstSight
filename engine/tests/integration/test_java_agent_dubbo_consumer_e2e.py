"""真实 Dubbo 消费端代理下的 Filter 短路：普通 classpath 与 Spring Boot fat jar。

示例程序 `java/samples/dubbo-consumer` 用 `ReferenceConfig` 直连
`dubbo://127.0.0.1:<计数端口>`，不启注册中心，也不启 provider。Python 侧在
该端口监听并记录 accept 次数与收到的字节数：Filter 命中桩时不建连、不发请求。

启用条件：

- `ASTSIGHT_JAVA_AGENT_E2E=1`
- 已构建 `java/astsight-agent/target/astsight-agent.jar`
- 已构建样例：
  `cd java/samples/dubbo-consumer && mvn -q -DskipTests package`
  （产出 `target/classes`、`target/consumer-classpath.txt` 与 fat jar）
- JDK（需能运行 Java 17 字节码）
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from astsight.mock import MockHttpServer
from contracts import MockStub

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_AGENT_E2E") != "1",
    reason="Java agent 联调默认跳过：设置 ASTSIGHT_JAVA_AGENT_E2E=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "java" / "samples" / "dubbo-consumer"
TOKEN = "agent-dubbo-consumer-e2e-token"
DEP_ID = (
    "dubbo:io.astsight.sample.consumer.OrderApi#create(java.lang.String)"
    "?group=order&version=1.0.0"
)


def _executable(path: Path) -> Path | None:
    if path.is_file():
        return path
    if os.name == "nt":
        exe = Path(str(path) + ".exe")
        if exe.is_file():
            return exe
    return None


def _java_tool(name: str) -> Path:
    configured = os.environ.get("ASTSIGHT_JAVA_AGENT_E2E_JAVA", "").strip()
    if configured:
        candidate = Path(configured)
        tool = candidate / "bin" / name if candidate.is_dir() else candidate
        resolved = _executable(tool)
        if resolved is not None:
            return resolved
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        resolved = _executable(Path(home) / "bin" / name)
        if resolved is not None:
            return resolved
    found = shutil.which(name)
    if found:
        return Path(found)
    pytest.skip(f"未找到 {name}；设置 ASTSIGHT_JAVA_AGENT_E2E_JAVA 或 JAVA_HOME")


def _agent_jar() -> Path:
    path = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not path.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests package")
    return path


def _consumer_classpath() -> str:
    path = SAMPLE / "target" / "consumer-classpath.txt"
    if not path.is_file():
        pytest.skip(
            "消费端样例未构建：cd java/samples/dubbo-consumer && "
            "mvn -q -DskipTests package"
        )
    parts = [line.strip() for line in path.read_text(encoding="utf-8").split(os.pathsep)]
    return os.pathsep.join(part for part in parts if part)


def _consumer_classes() -> Path:
    path = SAMPLE / "target" / "classes"
    if not path.is_dir():
        pytest.skip(
            "消费端样例未构建：cd java/samples/dubbo-consumer && "
            "mvn -q -DskipTests package"
        )
    return path


def _fat_jar() -> Path:
    candidates = [
        path
        for path in (SAMPLE / "target").glob("dubbo-consumer-sample-*.jar")
        if not path.name.endswith(".original")
    ]
    if not candidates:
        pytest.skip(
            "消费端 fat jar 未构建：cd java/samples/dubbo-consumer && "
            "mvn -q -DskipTests package"
        )
    return candidates[0]


class CountingListener:
    """在目标端口记录建连与收到的字节；收到字节说明真实 RPC 请求发生了。"""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = int(self._socket.getsockname()[1])
        self.accepts = 0
        self.bytes_received = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "CountingListener":
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                self.accepts += 1
                connection.settimeout(0.2)
                try:
                    payload = connection.recv(4096)
                    self.bytes_received += len(payload)
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._socket.close()


def _session(endpoint: str, sut_id: str, session_id: str) -> None:
    response = httpx.post(
        f"{endpoint}/_admin/sessions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "session_id": session_id,
            "namespace": sut_id,
            "case_id": session_id,
            "ttl_ms": 60_000,
            "bindings": [{"dep_id": DEP_ID, "branch": "success"}],
        },
        timeout=5,
    )
    assert response.status_code == 200, response.text


def _heartbeat(endpoint: str, *, timeout: float = 5.0) -> dict:
    admin = {"Authorization": f"Bearer {TOKEN}"}
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{endpoint}/_admin/agent/heartbeat", headers=admin, timeout=5
        )
        if response.status_code == 200:
            last = response.json()
            dubbo = last.get("dubbo") or {}
            if dubbo.get("filter_instantiated", 0) >= 1 and dubbo.get(
                "filter_hits", 0
            ) >= 1:
                return last
        time.sleep(0.2)
    assert last is not None, "agent 未上报心跳"
    return last


def _agent_args(*, endpoint: str, sut_id: str) -> str:
    return (
        f"admin={endpoint},token={TOKEN},sut_id={sut_id},"
        "heartbeat_interval_ms=500,route_refresh_interval_ms=500"
    )


def _run(
    command: list[str],
    *,
    endpoint: str,
    sut_id: str,
    port: int,
) -> subprocess.CompletedProcess[str]:
    java = _java_tool("java")
    jar = _agent_jar()
    return subprocess.run(
        [
            str(java),
            f"-javaagent:{jar}={_agent_args(endpoint=endpoint, sut_id=sut_id)}",
            *command,
            f"--port={port}",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )


def _assert_loader(output: str, expected: str) -> None:
    loader = None
    filter_loader = None
    for line in output.splitlines():
        if line.startswith("loader="):
            loader = line[len("loader="):]
        elif line.startswith("filter_loader="):
            filter_loader = line[len("filter_loader="):]
    assert loader is not None, output
    assert expected in loader, output
    assert filter_loader == loader, output


def _assert_short_circuit(
    completed: subprocess.CompletedProcess[str],
    *,
    listener: CountingListener,
    server: MockHttpServer,
    session_id: str,
) -> None:
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = completed.stdout
    assert "result=O-1,10" in output, output
    assert "error=" not in output, output
    # 真实代理调用没有建连、也没有发出任何字节。
    assert listener.accepts == 0, output
    assert listener.bytes_received == 0, output

    records = server.evidence.records
    assert len(records) == 1, [record.model_dump() for record in records]
    record = records[0]
    assert record.dep_id == DEP_ID
    assert record.adapter == "dubbo-filter"
    assert record.mode == "agent_filter"
    assert record.branch == "success"
    assert record.result.value == "ok"
    assert record.session_id == session_id
    assert record.original_path == (
        "dubbo:io.astsight.sample.consumer.OrderApi#create"
    )

    heartbeat = _heartbeat(server.endpoint)
    dubbo = heartbeat["dubbo"]
    assert dubbo["installed"] is True
    assert dubbo["version"] == "2.7.22"
    assert dubbo["filter_instantiated"] >= 1
    assert dubbo["filter_hits"] >= 1
    assert dubbo["serialization_verified"] is False


def _server(tmp_path: Path, sut_id: str) -> MockHttpServer:
    server = MockHttpServer(
        sut_id=sut_id,
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dubbo_dep(
        DEP_ID,
        branches={"success": MockStub(body={"orderId": "O-1", "amount": 10})},
    )
    server.start()
    return server


def test_real_reference_short_circuits_on_plain_classpath(tmp_path: Path) -> None:
    classes = _consumer_classes()
    classpath = _consumer_classpath()
    sut_id = "sut-dubbo-consumer-cp"
    server = _server(tmp_path, sut_id)
    listener = CountingListener().start()
    try:
        _session(server.endpoint, sut_id, "session-consumer-cp")
        completed = _run(
            [
                "-cp",
                os.pathsep.join([str(classes), classpath]),
                "io.astsight.sample.consumer.DubboConsumerSample",
            ],
            endpoint=server.endpoint,
            sut_id=sut_id,
            port=listener.port,
        )
        _assert_short_circuit(
            completed,
            listener=listener,
            server=server,
            session_id="session-consumer-cp",
        )
        _assert_loader(completed.stdout, "AppClassLoader")
    finally:
        listener.stop()
        server.stop()


def test_real_reference_short_circuits_in_spring_boot_fat_jar(tmp_path: Path) -> None:
    jar = _fat_jar()
    sut_id = "sut-dubbo-consumer-boot"
    server = _server(tmp_path, sut_id)
    listener = CountingListener().start()
    try:
        _session(server.endpoint, sut_id, "session-consumer-boot")
        completed = _run(
            ["-jar", str(jar)],
            endpoint=server.endpoint,
            sut_id=sut_id,
            port=listener.port,
        )
        _assert_short_circuit(
            completed,
            listener=listener,
            server=server,
            session_id="session-consumer-boot",
        )
        # Spring Boot fat jar 下应用代码与 Dubbo 都在 LaunchedClassLoader
        # （3.x 旧名为 LaunchedURLClassLoader），Filter 必须由同一个加载器
        # 定义才能看到 Dubbo 接口。
        _assert_loader(completed.stdout, "org.springframework.boot.loader")
    finally:
        listener.stop()
        server.stop()
