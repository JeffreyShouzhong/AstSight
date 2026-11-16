"""Java HTTP agent P0 sparse：OkHttp + HTTPS URL 改写 → Mock 证据。

默认跳过。启用条件：

- `ASTSIGHT_JAVA_AGENT_E2E=1`
- 已执行 `cd java && mvn -q -DskipTests package`，存在
  `java/astsight-agent/target/astsight-agent.jar`
- `ASTSIGHT_JAVA_AGENT_E2E_JAVA` / `JAVA_HOME` / PATH 中能找到 JDK（需 javac）
- 本机 Maven 仓库有 OkHttp 3.14.9 与 Okio 1.17.2（`mvn package` 会下载）

验证：agent 把 `https://api.example.com/orders/42?trace=1` 改写为
`http://127.0.0.1:<port>/_dep/<route_key>/orders/42?trace=1`，Mock 命中分支并
在 `mock_calls.jsonl` 写入 `adapter=okhttp`、`mode=agent_rewrite`、
`original_host=api.example.com`，心跳计数同步增加。
"""
from __future__ import annotations

import os
import shutil
import subprocess
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
TOKEN = "agent-e2e-token"
DEP_ID = "http:host:api.example.com GET /orders/{}"


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


def test_https_request_is_rewritten_and_evidenced(tmp_path: Path) -> None:
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests package")
    java = _java_tool("java")
    javac = _java_tool("javac")
    okhttp = _jar("com/squareup/okhttp3/okhttp/3.14.9/okhttp-3.14.9.jar")
    okio = _jar("com/squareup/okio/okio/1.17.2/okio-1.17.2.jar")
    sample = ROOT / "java" / "samples" / "OkHttpSample.java"
    if not sample.is_file():
        pytest.skip(f"缺少示例源码 {sample}")

    dep = DepIdEntry(
        dep_id=DEP_ID,
        downstream_key="host:api.example.com",
        evidence="host",
        method="GET",
        path_key="/orders/{}",
        target="https://api.example.com/orders/{}",
        route_host="api.example.com",
    )
    route_key = assign_route_keys([dep])[dep.dep_id]
    server = MockHttpServer(
        sut_id="sut-agent",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={"success": MockStub(body={"ok": True, "route": route_key})},
    )
    server.start()
    try:
        admin = {"Authorization": f"Bearer {TOKEN}"}
        with httpx.Client(base_url=server.endpoint) as client:
            created = client.post(
                "/_admin/sessions",
                headers=admin,
                json={
                    "session_id": "agent-e2e-s1",
                    "namespace": "sut-agent",
                    "case_id": "AGENT-E2E-1",
                    "run_scope": "R1",
                    "bindings": [{"dep_id": dep.dep_id, "branch": "success"}],
                },
            )
            assert created.status_code == 200, created.text

        classes = tmp_path / "classes"
        classes.mkdir()
        compile_cp = os.pathsep.join([str(okhttp), str(okio)])
        subprocess.run(
            [str(javac), "-cp", compile_cp, "-d", str(classes), str(sample)],
            check=True,
            capture_output=True,
            timeout=120,
        )

        java_args = (
            f"admin={server.endpoint},token={TOKEN},sut_id=sut-agent,"
            "heartbeat_interval_ms=1000,route_refresh_interval_ms=1000"
        )
        run_cp = os.pathsep.join([str(classes), str(okhttp), str(okio)])
        completed = subprocess.run(
            [
                str(java),
                f"-javaagent:{agent}={java_args}",
                "-cp",
                run_cp,
                "OkHttpSample",
                "https://api.example.com/orders/42?trace=1",
                "1500",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert "status=200" in completed.stdout
        assert '"ok":true' in completed.stdout

        deadline = time.monotonic() + 5
        heartbeat = None
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{server.endpoint}/_admin/agent/heartbeat",
                headers=admin,
                timeout=5,
            )
            if response.status_code == 200:
                heartbeat = response.json()
                if heartbeat.get("http_rewrites", 0) >= 1:
                    break
            time.sleep(0.2)
        assert heartbeat is not None
        adapter_ids = {item["id"] for item in heartbeat["http_adapters"]}
        assert "okhttp" in adapter_ids
        assert heartbeat["http_hooked"] >= 1
        assert heartbeat["http_rewrites"] >= 1
        assert heartbeat["http_errors"] == 0

        calls = server.evidence.records
        assert len(calls) == 1
        call = calls[0]
        assert call.result == "ok"
        assert call.route_key == route_key
        assert call.branch == "success"
        assert call.original_path == "/orders/42"
        assert call.query_keys == ["trace"]
        assert call.adapter == "okhttp"
        assert call.mode == "agent_rewrite"
        assert call.original_host == "api.example.com"
    finally:
        server.stop()
