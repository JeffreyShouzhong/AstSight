"""HTTP/2 prior-knowledge（h2c）负例：改写发生、请求显式失败、零透传。

Mock 与 agent 都只支持 HTTP/1.1。OkHttp 配置 `Protocol.H2_PRIOR_KNOWLEDGE`
后不打招呼直接发明文 HTTP/2 preface，因此：

- agent 仍然命中路由并改写 URL（`http_rewrites >= 1`），不会因为协议而放弃；
- 改写后的请求在 Mock 侧无法完成 HTTP 交换（Mock 不解析 h2c），SUT 得到
  IOException 并以非 0 退出；
- 真实下游（用一个只计数连接的本地 socket 代替）收到 0 次连接：失败关闭、
  零透传，而不是悄悄回落到真实下游。

默认跳过。启用条件同其它 agent e2e：

- `ASTSIGHT_JAVA_AGENT_E2E=1`
- 已构建 `java/astsight-agent/target/astsight-agent.jar`
- JDK（需 javac）与 Maven 仓库中的 OkHttp 3.14.9 / Okio 1.17.2
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

from astsight.mock import DepIdEntry, MockHttpServer, assign_route_keys
from contracts import MockStub

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_AGENT_E2E") != "1",
    reason="Java agent 联调默认跳过：设置 ASTSIGHT_JAVA_AGENT_E2E=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "agent-h2-e2e-token"


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


def _jar(relative: str) -> Path:
    path = Path.home() / ".m2" / "repository" / relative
    if not path.is_file():
        pytest.skip(f"Maven 仓库缺少 {path}；先执行 java/mvn package")
    return path


class _CountingListener:
    """只接受并计数 TCP 连接，用于证明没有真实下游流量。"""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = int(self._sock.getsockname()[1])
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def _heartbeat(endpoint: str, timeout: float = 5.0) -> dict:
    admin = {"Authorization": f"Bearer {TOKEN}"}
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{endpoint}/_admin/agent/heartbeat", headers=admin, timeout=5
        )
        if response.status_code == 200:
            last = response.json()
            if last.get("http_rewrites", 0) >= 1:
                return last
        time.sleep(0.2)
    assert last is not None, "agent 未上报心跳"
    return last


def test_h2_prior_knowledge_fails_closed_without_passthrough(tmp_path: Path) -> None:
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests package")
    java = _java_tool("java")
    javac = _java_tool("javac")
    okhttp = _jar("com/squareup/okhttp3/okhttp/3.14.9/okhttp-3.14.9.jar")
    okio = _jar("com/squareup/okio/okio/1.17.2/okio-1.17.2.jar")
    sample = ROOT / "java" / "samples" / "OkHttpH2PriorKnowledgeSample.java"
    if not sample.is_file():
        pytest.skip(f"缺少示例源码 {sample}")

    real = _CountingListener()
    dep = DepIdEntry(
        dep_id=f"http:host:127.0.0.1:{real.port} GET /orders/{{}}",
        downstream_key=f"host:127.0.0.1:{real.port}",
        evidence="host",
        method="GET",
        path_key="/orders/{}",
        target=f"http://127.0.0.1:{real.port}/orders/{{}}",
        route_host=f"127.0.0.1:{real.port}",
    )
    route_key = assign_route_keys([dep])[dep.dep_id]
    server = MockHttpServer(
        sut_id="sut-h2",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={"success": MockStub(body={"ok": True})},
    )
    server.start()
    try:
        classes = tmp_path / "classes"
        classes.mkdir()
        compile_cp = os.pathsep.join([str(okhttp), str(okio)])
        subprocess.run(
            [
                str(javac),
                "-encoding",
                "UTF-8",
                "-cp",
                compile_cp,
                "-d",
                str(classes),
                str(sample),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

        java_args = (
            f"admin={server.endpoint},token={TOKEN},sut_id=sut-h2,"
            "heartbeat_interval_ms=1000,route_refresh_interval_ms=1000"
        )
        run_cp = os.pathsep.join([str(classes), str(okhttp), str(okio)])
        completed = subprocess.run(
            [
                str(java),
                f"-javaagent:{agent}={java_args}",
                "-cp",
                run_cp,
                "OkHttpH2PriorKnowledgeSample",
                f"http://127.0.0.1:{real.port}/orders/42?h2=1",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # h2c 到 HTTP/1.1 Mock 无法完成交换：显式失败，不是伪装成功。
        assert completed.returncode != 0, completed.stdout + completed.stderr
        assert "failed=" in completed.stdout, completed.stdout + completed.stderr
        assert "status=" not in completed.stdout

        time.sleep(0.3)
        assert real.connections == 0, "请求被透传到真实下游"

        heartbeat = _heartbeat(server.endpoint)
        adapter_ids = {item["id"] for item in heartbeat["http_adapters"]}
        assert "okhttp" in adapter_ids
        assert heartbeat["http_hooked"] >= 1
        assert heartbeat["http_rewrites"] >= 1
        assert heartbeat["http_passthrough"] == 0
        # 改写后的 h2c 请求无法被 Mock 解析，因此没有 HTTP 层证据。
        assert server.evidence.records == []
    finally:
        server.stop()
        real.close()
