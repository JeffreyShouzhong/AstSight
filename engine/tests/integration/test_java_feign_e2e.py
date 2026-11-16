"""真实 Spring Cloud 工程端到端：Feign → discovery_override → Mock。

默认跳过。启用条件：

- `ASTSIGHT_JAVA_E2E=1`
- `ASTSIGHT_JAVA_E2E_PROJECT=<feign-eureka 工程根目录>`，且 client 模块已构建
  （`mvn -q -pl client -am package -DskipTests`）
- `java` 在 PATH、`JAVA_HOME` 或 `ASTSIGHT_JAVA_E2E_JAVA` 中可用

验证的是确定性链路：扫描 A 的 Feign 调用点 → 生成 MockPlan →
`SPRING_APPLICATION_JSON` 静态实例 → SUT 真实启动 → 请求命中 Mock →
`mock_calls.jsonl` 留证；另有一条反证锁住服务名大小写语义。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from astsight.agent.mockplan import build_mock_plan
from astsight.agent.sut import start_sut
from astsight.mock import start_mock_plan
from astsight.profile.downstream import DownstreamScanner
from contracts import MockBranch, MockDep, MockSemantic, MockStub

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_E2E") != "1",
    reason="真实 Java 联调默认跳过：设置 ASTSIGHT_JAVA_E2E=1 启用",
)

HANDLER = "HelloClientApplication.hello"
SOURCE = "src/main/java/demo/HelloClientApplication.java"
MOCK_BODY = "pipeline-mocked"
SUT_TIMEOUT = 180.0


def _sample_dir() -> Path:
    raw = os.environ.get("ASTSIGHT_JAVA_E2E_PROJECT", "").strip()
    if not raw:
        pytest.skip("ASTSIGHT_JAVA_E2E_PROJECT 未配置（指向 feign-eureka 工程根目录）")
    root = Path(raw)
    client = root / "client" if (root / "client").is_dir() else root
    if not (client / "pom.xml").is_file():
        pytest.skip(f"{client} 不像 Spring Cloud 样例的 client 模块")
    return client


def _java() -> str:
    configured = os.environ.get("ASTSIGHT_JAVA_E2E_JAVA", "").strip()
    if configured:
        return configured
    found = shutil.which("java")
    if found:
        return found
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        candidate = Path(home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            return str(candidate)
    pytest.skip("找不到 java：设置 ASTSIGHT_JAVA_E2E_JAVA 或 JAVA_HOME")


def _client_jar(client: Path) -> Path:
    target = client / "target"
    jars = sorted(
        target.glob("*.jar"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not jars:
        pytest.skip(
            f"{target} 下没有 jar：先运行 mvn -q -pl client -am package -DskipTests"
        )
    return jars[0]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scan_dep(client: Path) -> dict:
    scan = DownstreamScanner(client).scan(HANDLER, file_hint=SOURCE)
    assert scan.checked is True
    for dep in scan.deps:
        if dep.get("method") == "GET" and dep.get("path_key") == "/":
            return dep
    raise AssertionError(f"未识别出 HelloServer 的 Feign 调用点：{scan.deps}")


def _context(dep: dict) -> dict:
    return {
        "context_id": "java-e2e",
        "profiles": [
            {
                "endpoint": "GET /",
                "downstream_deps": [dep],
            }
        ],
    }


def _semantic(dep_id: str) -> MockSemantic:
    return MockSemantic(
        schema_version=1,
        deps=[
            MockDep(
                dep_id=dep_id,
                endpoint="GET /",
                branches=[
                    MockBranch(
                        branch="success",
                        semantics="真实 Spring Cloud 联调的固定桩响应",
                        stub=MockStub(status=200, body=MOCK_BODY),
                    )
                ],
            )
        ],
    )


def _draft(dep_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        cases=[SimpleNamespace(mock_bindings={dep_id: "success"})]
    )


def _start_plan(dep: dict, tmp_path: Path):
    """走真实的 build_mock_plan → start_mock_plan，不手写计划 JSON。"""
    dep_id = str(dep["dep_id"])
    plan, _warnings = build_mock_plan(
        _context(dep),
        _semantic(dep_id),
        _draft(dep_id),
        sut_id="java-e2e",
    )
    assert plan is not None
    plan.port = 0
    plan_path = tmp_path / "mock_plan.json"
    plan_path.write_text(plan.model_dump_json(exclude_none=True), encoding="utf-8")
    return start_mock_plan(plan_path, history_path=tmp_path / "mock_calls.jsonl")


def _open_session(runtime, dep_id: str) -> None:
    """Mock 按用例会话取分支；没有会话时请求会被 502 拒绝。"""
    response = httpx.post(
        f"{runtime.server.admin_endpoint}/_admin/sessions",
        headers={"Authorization": f"Bearer {runtime.token}"},
        json={
            "session_id": "java-e2e",
            "namespace": runtime.plan.namespace or "java-e2e",
            "case_id": "TC-JAVA-E2E",
            "bindings": [{"dep_id": dep_id, "branch": "success"}],
        },
        timeout=5,
    )
    assert response.status_code == 200, response.text


def _records(runtime, *, timeout: float = 3.0) -> list[dict]:
    path = runtime.evidence_path
    assert path is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.1)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _start_sut(
    jar: Path,
    document: dict,
    tmp_path: Path,
    *,
    name: str,
):
    port = _free_port()
    return start_sut(
        [_java(), "-jar", str(jar)],
        base_url=f"http://127.0.0.1:{port}",
        log_path=tmp_path / f"sut-{name}.log",
        injection_document=document,
        extra_env={"SERVER_PORT": str(port)},
        timeout=SUT_TIMEOUT,
    )


def test_feign_discovery_override_end_to_end(tmp_path: Path) -> None:
    client = _sample_dir()
    jar = _client_jar(client)
    dep = _scan_dep(client)
    assert dep["dep_id"] == "http:svc:helloserver GET /"
    assert dep["service_name"] == "HelloServer"

    runtime = _start_plan(dep, tmp_path)
    try:
        document = runtime.injection_document()
        injection = document["injections"][0]
        assert injection["strategy"] == "discovery_override"
        assert injection["service_name"] == "HelloServer"

        _open_session(runtime, str(dep["dep_id"]))
        sut = _start_sut(jar, document, tmp_path, name="ok")
        try:
            assert "SPRING_APPLICATION_JSON" in sut.env_keys
            response = httpx.get(f"{sut.base_url}/", timeout=15)
            assert response.status_code == 200, (
                f"{response.text}\n{sut.log_path.read_text(encoding='utf-8', errors='replace')[-4000:]}"
            )
            assert response.text == MOCK_BODY

            records = _records(runtime)
            assert any(
                record.get("route_match") == "method_path"
                and record.get("branch") == "success"
                and record.get("result") == "ok"
                for record in records
            ), records
        finally:
            sut.stop()
    finally:
        runtime.stop()


def test_discovery_service_name_is_case_sensitive(tmp_path: Path) -> None:
    """反证：服务名被小写化后 Spring Cloud 解析不到实例，SUT 失败且 Mock 无调用。"""
    client = _sample_dir()
    jar = _client_jar(client)
    dep = _scan_dep(client)

    runtime = _start_plan(dep, tmp_path)
    try:
        document = runtime.injection_document()
        document["injections"][0]["service_name"] = "helloserver"

        _open_session(runtime, str(dep["dep_id"]))
        sut = _start_sut(jar, document, tmp_path, name="lowercase")
        try:
            response = httpx.get(f"{sut.base_url}/", timeout=15)
            assert response.status_code == 500
        finally:
            sut.stop()

        records = _records(runtime, timeout=1.0)
        assert records == [], records
    finally:
        runtime.stop()
