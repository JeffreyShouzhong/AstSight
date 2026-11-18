"""HTTP agent 安全矩阵：连接复用多次调用与重定向失败关闭。"""
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
TOKEN = "agent-safety-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}


def _executable(path: Path) -> Path | None:
    if path.is_file():
        return path
    if os.name == "nt" and Path(str(path) + ".exe").is_file():
        return Path(str(path) + ".exe")
    return None


def _java_tool(name: str) -> Path:
    configured = os.environ.get("ASTSIGHT_JAVA_AGENT_E2E_JAVA", "").strip()
    candidates = []
    if configured:
        candidate = Path(configured)
        candidates.append(
            candidate / "bin" / name if candidate.is_dir() else candidate
        )
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / name)
    if shutil.which(name):
        candidates.append(Path(shutil.which(name)))
    for candidate in candidates:
        resolved = _executable(candidate)
        if resolved is not None:
            return resolved
    pytest.skip(f"未找到 {name}")


def _jar(relative: str) -> Path:
    path = Path.home() / ".m2" / "repository" / relative
    if not path.is_file():
        pytest.skip(f"Maven 仓库缺少 {path}")
    return path


class _CountingListener:
    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = int(self._socket.getsockname()[1])
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def _dep(port: int) -> DepIdEntry:
    return DepIdEntry(
        dep_id=f"http:host:127.0.0.1:{port} GET /orders/{{}}",
        downstream_key=f"host:127.0.0.1:{port}",
        evidence="host",
        method="GET",
        path_key="/orders/{}",
        target=f"http://127.0.0.1:{port}/orders/{{}}",
        route_host=f"127.0.0.1:{port}",
    )


def _compile_sample(tmp_path: Path) -> Path:
    javac = _java_tool("javac")
    okhttp = _jar("com/squareup/okhttp3/okhttp/3.14.9/okhttp-3.14.9.jar")
    okio = _jar("com/squareup/okio/okio/1.17.2/okio-1.17.2.jar")
    sample = ROOT / "java" / "samples" / "OkHttpSafetySample.java"
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(
        [
            str(javac),
            "-encoding",
            "UTF-8",
            "-cp",
            os.pathsep.join([str(okhttp), str(okio)]),
            "-d",
            str(classes),
            str(sample),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return classes


def _run(
    *,
    server: MockHttpServer,
    classes: Path,
    target: str,
    mode: str,
    count: int = 3,
) -> subprocess.CompletedProcess:
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建")
    okhttp = _jar("com/squareup/okhttp3/okhttp/3.14.9/okhttp-3.14.9.jar")
    okio = _jar("com/squareup/okio/okio/1.17.2/okio-1.17.2.jar")
    java_args = (
        f"admin={server.endpoint},token={TOKEN},sut_id={server.sut_id},"
        "heartbeat_interval_ms=500,route_refresh_interval_ms=500"
    )
    classpath = os.pathsep.join(
        [str(classes), str(okhttp), str(okio)]
    )
    return subprocess.run(
        [
            str(_java_tool("java")),
            f"-javaagent:{agent}={java_args}",
            "-cp",
            classpath,
            "OkHttpSafetySample",
            target,
            mode,
            str(count),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _start_server(tmp_path: Path, dep: DepIdEntry, branches: dict) -> MockHttpServer:
    server = MockHttpServer(
        sut_id="sut-safety",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    route_key = assign_route_keys([dep])[dep.dep_id]
    server.add_dep(dep, route_key=route_key, branches=branches)
    server.start()
    response = httpx.post(
        f"{server.endpoint}/_admin/sessions",
        headers=ADMIN,
        json={
            "session_id": "safety-s1",
            "namespace": server.namespace,
            "case_id": "SAFETY-1",
            "bindings": [{"dep_id": dep.dep_id, "branch": "success"}],
        },
        timeout=5,
    )
    assert response.status_code == 200, response.text
    return server


def test_connection_reuse_multiple_calls_all_hit_mock(tmp_path: Path) -> None:
    real = _CountingListener()
    dep = _dep(real.port)
    server = _start_server(
        tmp_path,
        dep,
        {"success": MockStub(body={"ok": True})},
    )
    try:
        classes = _compile_sample(tmp_path)
        completed = _run(
            server=server,
            classes=classes,
            target=f"http://127.0.0.1:{real.port}/orders/42",
            mode="multi",
            count=3,
        )
        assert completed.returncode == 0, completed.stderr
        assert "success=3" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 3
        assert all(call.result == "ok" for call in server.evidence.records)
        assert all(
            call.original_host == f"127.0.0.1:{real.port}"
            for call in server.evidence.records
        )
    finally:
        server.stop()
        real.close()


def test_mock_redirect_is_blocked_before_client_can_follow(tmp_path: Path) -> None:
    real = _CountingListener()
    dep = _dep(real.port)
    server = _start_server(
        tmp_path,
        dep,
        {
            "success": MockStub(
                status=302,
                headers={
                    "Location": f"http://127.0.0.1:{real.port}/leak"
                },
            )
        },
    )
    try:
        classes = _compile_sample(tmp_path)
        completed = _run(
            server=server,
            classes=classes,
            target=f"http://127.0.0.1:{real.port}/orders/42",
            mode="redirect",
        )
        assert completed.returncode == 0, completed.stderr
        assert "status=502" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 1
        call = server.evidence.records[0]
        assert call.error == "mock_redirect_unsupported"
        assert call.result == "unbound"
    finally:
        server.stop()
        real.close()
