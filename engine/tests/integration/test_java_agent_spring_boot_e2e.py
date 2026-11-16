"""Spring Boot fat jar 下的 agent 改写：OkHttp / Feign / RestTemplate → Mock。

默认跳过。启用条件：

- `ASTSIGHT_JAVA_AGENT_SPRING_E2E=1`
- 已构建 `java/astsight-agent/target/astsight-agent.jar`
- 已构建 `java/samples/spring-boot-okhttp/target/*.jar`
- `ASTSIGHT_JAVA_AGENT_E2E_JAVA` / `JAVA_HOME` / PATH 中有 JDK

验证重点：三个 adapter 在 `LaunchedURLClassLoader` 下都能改写 HTTPS 目标，
且 OkHttp 的 ASM + 反射 bridge 不触发 `NoClassDefFoundError`；请求命中 Mock、
证据分别记录 adapter，心跳计数正确。
"""
from __future__ import annotations

import os
import shutil
import socket
import time
from pathlib import Path

import httpx
import pytest

from astsight.agent.sut import start_sut
from astsight.mock import DepIdEntry, MockHttpServer, assign_route_keys
from contracts import MockStub

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_AGENT_SPRING_E2E") != "1",
    reason="Spring Boot agent 联调默认跳过：设置 ASTSIGHT_JAVA_AGENT_SPRING_E2E=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "spring-agent-e2e"
DEP_ID = "http:host:api.example.com GET /orders/{}"


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
        resolved = _executable(candidate / "bin" / "java" if candidate.is_dir() else candidate)
        if resolved is not None:
            return resolved
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        resolved = _executable(Path(home) / "bin" / "java")
        if resolved is not None:
            return resolved
    found = shutil.which("java")
    if found:
        return Path(found)
    pytest.skip("未找到 java；设置 ASTSIGHT_JAVA_AGENT_E2E_JAVA 或 JAVA_HOME")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_spring_boot_fat_jar_rewrites_https_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests clean package")
    samples = sorted(
        (ROOT / "java" / "samples" / "spring-boot-okhttp" / "target").glob(
            "spring-boot-okhttp-sample-*.jar"
        )
    )
    samples = [path for path in samples if not path.name.endswith(".original")]
    if not samples:
        pytest.skip(
            "Spring Boot 样例未构建：cd java/samples/spring-boot-okhttp && mvn -q -DskipTests package"
        )
    app_jar = samples[0]
    java = _java()

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
        sut_id="sut-boot",
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={"success": MockStub(body={"ok": True})},
    )
    server.start()
    sut = None
    try:
        admin = {"Authorization": f"Bearer {TOKEN}"}
        with httpx.Client(base_url=server.endpoint) as client:
            created = client.post(
                "/_admin/sessions",
                headers=admin,
                json={
                    "session_id": "boot-e2e-s1",
                    "namespace": "sut-boot",
                    "case_id": "BOOT-E2E-1",
                    "run_scope": "R1",
                    "bindings": [{"dep_id": dep.dep_id, "branch": "success"}],
                },
            )
            assert created.status_code == 200, created.text

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        # 走 `agent run` 真实使用的注入路径：JAVA_TOOL_OPTIONS + token_env。
        document = {
            "schema_version": 1,
            "sut_id": "sut-boot",
            "endpoint": server.endpoint,
            "injections": [
                {
                    "dep_id": dep.dep_id,
                    "strategy": "agent_rewrite",
                    "injected": server.endpoint,
                }
            ],
        }
        monkeypatch.setenv("ASTSIGHT_JAVA_AGENT_JAR", str(agent))
        sut = start_sut(
            [str(java), "-jar", str(app_jar), f"--server.port={port}"],
            base_url=base_url,
            log_path=tmp_path / "sut.log",
            injection_document=document,
            agent_token=TOKEN,
            timeout=90,
        )
        assert "JAVA_TOOL_OPTIONS" in sut.env_keys
        assert "ASTSIGHT_AGENT_TOKEN" in sut.env_keys
        assert any(
            warning.startswith("injection_agent_rewrite_applied")
            for warning in sut.warnings
        )
        log = (tmp_path / "sut.log").read_text(encoding="utf-8", errors="replace")
        assert "token_env=ASTSIGHT_AGENT_TOKEN" in log
        assert TOKEN not in log
        response = httpx.get(f"{base_url}/call", timeout=30)
        assert response.status_code == 200
        assert response.text.startswith("200:")
        assert '"ok":true' in response.text

        feign = httpx.get(f"{base_url}/feign/8", timeout=30)
        assert feign.status_code == 200, feign.text
        assert '"ok":true' in feign.text

        rest = httpx.get(f"{base_url}/rest/9", timeout=30)
        assert rest.status_code == 200, rest.text
        assert '"ok":true' in rest.text

        # 默认心跳间隔 5s；首次心跳在请求前发出，等下一轮拿到改写计数。
        deadline = time.monotonic() + 12
        heartbeat = None
        while time.monotonic() < deadline:
            payload = httpx.get(
                f"{server.endpoint}/_admin/agent/heartbeat",
                headers=admin,
                timeout=5,
            )
            if payload.status_code == 200:
                heartbeat = payload.json()
                if heartbeat.get("http_rewrites", 0) >= 3:
                    break
            time.sleep(0.2)
        assert heartbeat is not None
        adapters = {item["id"] for item in heartbeat["http_adapters"]}
        assert adapters == {"okhttp", "feign", "resttemplate"}
        assert heartbeat["http_hooked"] >= 3
        assert heartbeat["http_rewrites"] >= 3
        assert heartbeat["http_errors"] == 0

        calls = {call.original_path: call for call in server.evidence.records}
        assert set(calls) == {"/orders/7", "/orders/8", "/orders/9"}
        okhttp_call = calls["/orders/7"]
        assert okhttp_call.result == "ok"
        assert okhttp_call.query_keys == ["trace"]
        assert okhttp_call.adapter == "okhttp"
        assert okhttp_call.mode == "agent_rewrite"
        assert okhttp_call.original_host == "api.example.com"

        feign_call = calls["/orders/8"]
        assert feign_call.result == "ok"
        assert feign_call.query_keys == ["trace"]
        assert feign_call.adapter == "feign"
        assert feign_call.mode == "agent_rewrite"
        assert feign_call.original_host == "api.example.com"

        rest_call = calls["/orders/9"]
        assert rest_call.result == "ok"
        assert rest_call.query_keys == ["trace"]
        assert rest_call.adapter == "resttemplate"
        assert rest_call.mode == "agent_rewrite"
        assert rest_call.original_host == "api.example.com"
    finally:
        if sut is not None:
            sut.stop()
        server.stop()
