"""Dubbo 消费端 Filter 全链路：SPI 进链、按会话取桩、失败关闭、零真实调用。

不启动注册中心与 provider：示例程序直接经 Dubbo SPI 取出
`AstSightDubboFilter`，再对一个“假 Invoker”发起调用，真实调用会打印
`REAL_CALL` 并计数。这样能分别验证三条路径：

- 会话绑定命中：返回桩对象（Jackson 按接口返回类型构造），真实调用 0 次；
- 会话未绑定该 dep：抛 `RpcException(mock_unexpected_dep)`，真实调用 0 次；
- dep 不在计划内：放行真实调用（`REAL_CALL`），且不产生 Mock 证据。

默认跳过。启用条件：

- `ASTSIGHT_JAVA_AGENT_E2E=1`
- 已构建 `java/astsight-agent/target/astsight-agent.jar`
- 已构建 `java/astsight-agent-dubbo/target/dubbo-classpath.txt`（Maven
  `package` 阶段生成，含 Dubbo 2.7.22 及其依赖）
- JDK（需 javac）
"""
from __future__ import annotations

import os
import shutil
import subprocess
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
TOKEN = "agent-dubbo-e2e-token"
DEP_ID = "dubbo:OrderService#create?group=order&version=1.0.0"


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


def _dubbo_classpath() -> str:
    path = (
        ROOT
        / "java"
        / "astsight-agent-dubbo"
        / "target"
        / "dubbo-classpath.txt"
    )
    if not path.is_file():
        pytest.skip(
            "Dubbo classpath 未生成：cd java && mvn -q -DskipTests package"
        )
    parts = [line.strip() for line in path.read_text(encoding="utf-8").split(os.pathsep)]
    return os.pathsep.join(part for part in parts if part)


@pytest.fixture(scope="session")
def sample_classes(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    javac = _java_tool("javac")
    classpath = _dubbo_classpath()
    source = ROOT / "java" / "samples" / "DubboFilterSample.java"
    if not source.is_file():
        pytest.skip(f"缺少示例源码 {source}")
    classes = tmp_path_factory.mktemp("dubbo-sample")
    subprocess.run(
        [
            str(javac),
            "-encoding",
            "UTF-8",
            "-cp",
            classpath,
            "-d",
            str(classes),
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return classes, classpath


def _run_sample(
    classes: Path,
    classpath: str,
    mode: str,
    *,
    endpoint: str,
    sut_id: str,
) -> subprocess.CompletedProcess[str]:
    java = _java_tool("java")
    jar = _agent_jar()
    agent_args = (
        f"admin={endpoint},token={TOKEN},sut_id={sut_id},"
        "heartbeat_interval_ms=500,route_refresh_interval_ms=500"
    )
    return subprocess.run(
        [
            str(java),
            f"-javaagent:{jar}={agent_args}",
            "-cp",
            os.pathsep.join([str(classes), classpath]),
            "DubboFilterSample",
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


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


def test_dubbo_stub_returns_dto_without_real_call(
    sample_classes: tuple[Path, str], tmp_path: Path
) -> None:
    classes, classpath = sample_classes
    sut_id = "sut-dubbo-stub"
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
    try:
        # 会话 namespace 与 sut_id 一致。
        response = httpx.post(
            f"{server.endpoint}/_admin/sessions",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "session_id": "session-stub",
                "namespace": sut_id,
                "case_id": "case-stub",
                "ttl_ms": 60_000,
                "bindings": [{"dep_id": DEP_ID, "branch": "success"}],
            },
            timeout=5,
        )
        assert response.status_code == 200, response.text

        completed = _run_sample(
            classes, classpath, "stub", endpoint=server.endpoint, sut_id=sut_id
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        output = completed.stdout
        assert "filter=io.astsight.agent.dubbo.AstSightDubboFilter" in output
        assert "result=O-1,10" in output, output
        assert "REAL_CALL" not in output, output
        assert "real=0" in output, output

        records = server.evidence.records
        assert len(records) == 1, [record.model_dump() for record in records]
        record = records[0]
        assert record.dep_id == DEP_ID
        assert record.adapter == "dubbo-filter"
        assert record.mode == "agent_filter"
        assert record.branch == "success"
        assert record.result.value == "ok"
        assert record.session_id == "session-stub"
        assert record.original_path == "dubbo:OrderService#create"

        heartbeat = _heartbeat(server.endpoint)
        dubbo = heartbeat["dubbo"]
        assert dubbo["installed"] is True
        assert dubbo["version"] == "2.7.22"
        assert dubbo["filter_instantiated"] >= 1
        assert dubbo["filter_hits"] >= 1
        assert dubbo["serialization_verified"] is False
        assert heartbeat["http_rewrites"] == 0
    finally:
        server.stop()


def test_dubbo_unbound_dep_fails_closed(
    sample_classes: tuple[Path, str], tmp_path: Path
) -> None:
    classes, classpath = sample_classes
    sut_id = "sut-dubbo-unbound"
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
    try:
        response = httpx.post(
            f"{server.endpoint}/_admin/sessions",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "session_id": "session-unbound",
                "namespace": sut_id,
                "case_id": "case-unbound",
                "ttl_ms": 60_000,
                "bindings": [
                    {"dep_id": "dubbo:OtherService#other", "branch": "success"}
                ],
            },
            timeout=5,
        )
        assert response.status_code == 200, response.text

        completed = _run_sample(
            classes, classpath, "stub", endpoint=server.endpoint, sut_id=sut_id
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "error=astSight:mock_unexpected_dep" in completed.stdout
        assert "REAL_CALL" not in completed.stdout
        assert "real=0" in completed.stdout

        records = server.evidence.records
        assert len(records) == 1
        assert records[0].error == "mock_unexpected_dep"
        assert records[0].result.value == "unbound"
    finally:
        server.stop()


def test_dubbo_unplanned_dep_passes_through(
    sample_classes: tuple[Path, str], tmp_path: Path
) -> None:
    classes, classpath = sample_classes
    sut_id = "sut-dubbo-unplanned"
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
    try:
        response = httpx.post(
            f"{server.endpoint}/_admin/sessions",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "session_id": "session-unplanned",
                "namespace": sut_id,
                "case_id": "case-unplanned",
                "ttl_ms": 60_000,
                "bindings": [{"dep_id": DEP_ID, "branch": "success"}],
            },
            timeout=5,
        )
        assert response.status_code == 200, response.text

        completed = _run_sample(
            classes, classpath, "unplanned", endpoint=server.endpoint, sut_id=sut_id
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "REAL_CALL" in completed.stdout
        assert "result=REAL,0" in completed.stdout
        assert "real=1" in completed.stdout
        assert server.evidence.records == []

        heartbeat = _heartbeat(server.endpoint)
        assert heartbeat["dubbo"]["passthrough"] >= 1
    finally:
        server.stop()
