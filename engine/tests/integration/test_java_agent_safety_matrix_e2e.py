"""HTTP agent 安全矩阵：连接复用多次调用与重定向失败关闭。"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

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
    completed = subprocess.run(
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
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(f"javac 编译失败：\n{completed.stdout}\n{completed.stderr}")
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
    try:
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
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        pytest.fail(f"Java 样例超时：\nstdout={stdout}\nstderr={stderr}")


def _start_server(
    tmp_path: Path,
    dep: DepIdEntry,
    branches: dict,
    *,
    default_branch: str = "success",
    rules: list[dict] | None = None,
) -> MockHttpServer:
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
            "bindings": [
                {
                    "dep_id": dep.dep_id,
                    "branch": default_branch,
                    "rules": rules or [],
                }
            ],
        },
        timeout=5,
    )
    assert response.status_code == 200, response.text
    return server


def _heartbeat(server: MockHttpServer, *, rewrites: int = 1) -> dict:
    deadline = time.monotonic() + 10
    last: dict = {}
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{server.endpoint}/_admin/agent/heartbeat",
            headers=ADMIN,
            timeout=5,
        )
        if response.status_code == 200:
            last = response.json()
            if last.get("http_rewrites", 0) >= rewrites:
                return last
        time.sleep(0.2)
    assert last, "agent 未上报心跳"
    return last


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


def test_async_calls_all_hit_mock_without_real_connections(
    tmp_path: Path,
) -> None:
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
            mode="async",
            count=3,
        )
        assert completed.returncode == 0, completed.stderr
        assert "success=3" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 3
    finally:
        server.stop()
        real.close()


def test_internal_retry_reuses_rewritten_mock_target(tmp_path: Path) -> None:
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
            mode="retry",
        )
        assert completed.returncode == 0, completed.stderr
        assert "status=200" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 2
        heartbeat = _heartbeat(server)
        assert heartbeat["http_rewrites"] == 1
        assert heartbeat["http_passthrough"] == 0
    finally:
        server.stop()
        real.close()


def test_certificate_pinner_is_bypassed_only_by_explicit_mock_rewrite(
    tmp_path: Path,
) -> None:
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
            target=f"https://127.0.0.1:{real.port}/orders/42",
            mode="pinning",
        )
        assert completed.returncode == 0, completed.stderr
        assert "status=200" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 1
    finally:
        server.stop()
        real.close()


def test_client_scheme_guard_fails_closed_after_rewrite(tmp_path: Path) -> None:
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
            target=f"https://127.0.0.1:{real.port}/orders/42",
            mode="scheme_guard",
        )
        assert completed.returncode != 0
        assert "https_required" in completed.stderr
        assert real.connections == 0
        assert server.evidence.records == []
        assert _heartbeat(server)["http_rewrites"] >= 1
    finally:
        server.stop()
        real.close()


def test_already_rewritten_mock_url_is_not_rewritten_twice(
    tmp_path: Path,
) -> None:
    real = _CountingListener()
    dep = _dep(real.port)
    server = _start_server(
        tmp_path,
        dep,
        {"success": MockStub(body={"ok": True})},
    )
    route_key = assign_route_keys([dep])[dep.dep_id]
    try:
        classes = _compile_sample(tmp_path)
        completed = _run(
            server=server,
            classes=classes,
            target=(
                f"{server.endpoint}/_dep/{route_key}/orders/42"
            ),
            mode="direct",
        )
        assert completed.returncode == 0, completed.stderr
        assert "status=200" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 1
        assert server.evidence.records[0].adapter is None
    finally:
        server.stop()
        real.close()


def test_planned_host_unknown_path_fails_closed_with_evidence(
    tmp_path: Path,
) -> None:
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
            target=f"http://127.0.0.1:{real.port}/not-in-plan",
            mode="direct",
        )
        assert completed.returncode == 0, completed.stderr
        assert "status=502" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 1
        call = server.evidence.records[0]
        assert call.result == "unknown"
        assert call.error == "mock_route_unknown"
        assert call.original_path == "/not-in-plan"
        assert call.original_host == f"127.0.0.1:{real.port}"
        assert call.adapter == "okhttp"
        assert call.mode == "agent_rewrite"

        heartbeat = _heartbeat(server)
        assert heartbeat["http_passthrough"] == 0
        assert heartbeat["passthrough_hosts"] == {}
    finally:
        server.stop()
        real.close()


def test_signature_and_explicit_host_boundary_is_explicit(
    tmp_path: Path,
) -> None:
    real = _CountingListener()
    dep = _dep(real.port)
    route_key = assign_route_keys([dep])[dep.dep_id]
    server = MockHttpServer(
        sut_id="sut-safety",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={
            "success": MockStub(body={"ok": True}),
            "header_boundary_mismatch": MockStub(
                status=409,
                body={"ok": False},
            ),
        },
    )
    server.start()
    try:
        endpoint = urlsplit(server.endpoint)
        expected_canonical = (
            "GET|http|"
            f"{endpoint.hostname}|{endpoint.port}|"
            f"/_dep/{route_key}/orders/42|"
        )
        response = httpx.post(
            f"{server.endpoint}/_admin/sessions",
            headers=ADMIN,
            json={
                "session_id": "safety-s1",
                "namespace": server.namespace,
                "case_id": "SAFETY-1",
                "bindings": [
                    {
                        "dep_id": dep.dep_id,
                        "branch": "header_boundary_mismatch",
                        "rules": [
                            {
                                "branch": "success",
                                "match": {
                                    "headers": {
                                        "host": "downstream.example.test",
                                        "x-signature-precomputed": (
                                            "signed-before-agent"
                                        ),
                                        "x-test-canonical": expected_canonical,
                                    }
                                },
                            }
                        ],
                    }
                ],
            },
            timeout=5,
        )
        assert response.status_code == 200, response.text
        classes = _compile_sample(tmp_path)
        completed = _run(
            server=server,
            classes=classes,
            target=f"http://127.0.0.1:{real.port}/orders/42",
            mode="signature",
        )
        assert completed.returncode == 0, completed.stderr
        assert "status=200" in completed.stdout
        assert real.connections == 0
        assert len(server.evidence.records) == 1
        call = server.evidence.records[0]
        assert call.branch == "success"
        assert call.branch_source == "request_match"
    finally:
        server.stop()
        real.close()
