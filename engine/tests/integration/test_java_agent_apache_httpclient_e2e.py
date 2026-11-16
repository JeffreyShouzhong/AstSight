"""Java agent：直连 Apache HttpClient 4/5 的 HTTPS 改写 → Mock 证据。

默认跳过。启用条件：

- `ASTSIGHT_JAVA_AGENT_E2E=1`
- 已执行 `cd java && mvn -q -DskipTests package`，存在
  `java/astsight-agent/target/astsight-agent.jar`
- `ASTSIGHT_JAVA_AGENT_E2E_JAVA` / `JAVA_HOME` / PATH 中能找到 JDK（需 javac）
- 本机 Maven 仓库有 HttpClient 4.5.3 / 5.4.1 及其依赖。

验证 {@code InternalHttpClient#doExecute} 织入：两个样例分别用
`new HttpGet(https://...)` 直连，请求被改写到 `/_dep/<route_key>`，证据记录
`adapter=httpclient4/httpclient5` 与原始 host/path/query。
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


def test_direct_apache_httpclient_requests_are_rewritten(tmp_path: Path) -> None:
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests package")
    java = _java_tool("java")
    javac = _java_tool("javac")

    hc4 = [
        _jar("org/apache/httpcomponents/httpclient/4.5.3/httpclient-4.5.3.jar"),
        _jar("org/apache/httpcomponents/httpcore/4.4.16/httpcore-4.4.16.jar"),
        _jar("commons-logging/commons-logging/1.3.6/commons-logging-1.3.6.jar"),
    ]
    hc5 = [
        _jar("org/apache/httpcomponents/client5/httpclient5/5.4.1/httpclient5-5.4.1.jar"),
        _jar("org/apache/httpcomponents/core5/httpcore5/5.3.1/httpcore5-5.3.1.jar"),
        _jar("org/apache/httpcomponents/core5/httpcore5-h2/5.3.1/httpcore5-h2-5.3.1.jar"),
        _jar("org/slf4j/slf4j-api/1.7.36/slf4j-api-1.7.36.jar"),
    ]
    sample = ROOT / "java" / "samples" / "ApacheHttpClientSample.java"
    if not sample.is_file():
        pytest.skip(f"缺少示例源码 {sample}")
    jars = hc4 + hc5

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
        sut_id="sut-hc",
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
        admin = {"Authorization": f"Bearer {TOKEN}"}
        with httpx.Client(base_url=server.endpoint) as client:
            created = client.post(
                "/_admin/sessions",
                headers=admin,
                json={
                    "session_id": "hc-e2e-s1",
                    "namespace": "sut-hc",
                    "case_id": "HC-E2E-1",
                    "run_scope": "R1",
                    "bindings": [{"dep_id": dep.dep_id, "branch": "success"}],
                },
            )
            assert created.status_code == 200, created.text

        classes = tmp_path / "classes"
        classes.mkdir()
        java_args = (
            f"admin={server.endpoint},token={TOKEN},sut_id=sut-hc,"
            "heartbeat_interval_ms=1000,route_refresh_interval_ms=1000"
        )
        compile_cp = os.pathsep.join(str(path) for path in jars)
        subprocess.run(
            [str(javac), "-cp", compile_cp, "-d", str(classes), str(sample)],
            check=True,
            capture_output=True,
            timeout=120,
        )
        run_cp = os.pathsep.join([str(classes)] + [str(path) for path in jars])
        completed = subprocess.run(
            [
                str(java),
                f"-javaagent:{agent}={java_args}",
                "-cp",
                run_cp,
                "ApacheHttpClientSample",
                "https://api.example.com/orders/51?trace=hc4",
                "https://api.example.com/orders/52?trace=hc5",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, (
            f"退出码 {completed.returncode}\n{completed.stderr}"
        )
        assert "hc4 status=200" in completed.stdout, completed.stdout
        assert "hc5 status=200" in completed.stdout, completed.stdout
        assert completed.stdout.count('"ok":true') == 2, completed.stdout

        deadline = time.monotonic() + 10
        heartbeat = None
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{server.endpoint}/_admin/agent/heartbeat",
                headers=admin,
                timeout=5,
            )
            if response.status_code == 200:
                heartbeat = response.json()
                if heartbeat.get("http_rewrites", 0) >= 2:
                    break
            time.sleep(0.2)
        assert heartbeat is not None
        adapters = {item["id"] for item in heartbeat["http_adapters"]}
        assert {"httpclient4", "httpclient5"}.issubset(adapters)
        assert heartbeat["http_hooked"] >= 2
        assert heartbeat["http_rewrites"] >= 2
        assert heartbeat["http_errors"] == 0

        calls = {call.original_path: call for call in server.evidence.records}
        assert set(calls) == {"/orders/51", "/orders/52"}
        for path, adapter in (("/orders/51", "httpclient4"), ("/orders/52", "httpclient5")):
            call = calls[path]
            assert call.result == "ok"
            assert call.route_key == route_key
            assert call.branch == "success"
            assert call.query_keys == ["trace"]
            assert call.adapter == adapter
            assert call.mode == "agent_rewrite"
            assert call.original_host == "api.example.com"
    finally:
        server.stop()
