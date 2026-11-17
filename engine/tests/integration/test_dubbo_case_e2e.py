"""Dubbo 入口用例端到端：真 provider + sidecar + 生成的 pytest。

前置：

- `ASTSIGHT_DUBBO_SIDECAR_E2E=1`
- 已构建 `java/astsight-sidecar-dubbo`（`mvn -pl astsight-sidecar-dubbo package`）
- JDK 8+；provider 与 sidecar 都在本机

这个用例不碰 HTTP：`run_pytest` 只传 `ASTSIGHT_DUBBO_URL`，验证
`TestCase(transport=dubbo)` → conftest `dubbo_call` → sidecar 泛化调用 →
断言 `rpc_status/result_field` 的完整链路。
"""
from __future__ import annotations

import os
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from astsight.codegen import generate_pytest
from astsight.execute.runner import run_pytest
from astsight.pipeline import RunConfig, run
from astsight.sidecar import locate_java
from contracts import Assertion, DubboCall, TestCase, Transport

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "java" / "astsight-sidecar-dubbo"
JAR = MODULE / "target" / "astsight-dubbo-sidecar.jar"
CLASSPATH_FILE = MODULE / "target" / "sidecar-test-classpath.txt"
TEST_CLASSES = MODULE / "target" / "test-classes"
PROVIDER = "io.astsight.sidecar.dubbo.TestProviderMain"
ENDPOINT = (
    "dubbo:io.astsight.sidecar.dubbo.EchoService#describe"
    "(io.astsight.sidecar.dubbo.EchoRequest)?group=order&version=1.0.0"
)

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_DUBBO_SIDECAR_E2E") != "1",
    reason="Dubbo sidecar 联调默认跳过：设置 ASTSIGHT_DUBBO_SIDECAR_E2E=1 启用",
)


def _free_port() -> int:
    handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]
    finally:
        handle.close()


def _add_opens(java: str) -> list[str]:
    completed = subprocess.run(
        [java, "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    text = completed.stderr or completed.stdout
    major = 8
    for token in text.split('"'):
        head = token.split(".", 1)[0]
        if head.isdigit():
            major = int(head)
            break
    if major < 9:
        return []
    return [
        "--add-opens=java.base/java.lang=ALL-UNNAMED",
        "--add-opens=java.base/java.math=ALL-UNNAMED",
        "--add-opens=java.base/java.util=ALL-UNNAMED",
    ]


def _start_provider(java: str, port: int) -> subprocess.Popen[str]:
    classpath = os.pathsep.join(
        [str(TEST_CLASSES), CLASSPATH_FILE.read_text(encoding="utf-8").strip()]
    )
    process = subprocess.Popen(
        [
            java,
            *_add_opens(java),
            "-cp",
            classpath,
            PROVIDER,
            str(port),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: queue.Queue[str | None] = queue.Queue()
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        if line.strip().startswith("READY"):
            return process
    process.kill()
    process.wait(timeout=10)
    raise AssertionError(
        "Dubbo provider 未就绪：\n" + "\n".join(stderr_lines[-30:])
    )


def _case(*, target_port: int | None = None) -> TestCase:
    return TestCase(
        id="TC-0001",
        transport=Transport.DUBBO,
        endpoint=ENDPOINT,
        category="positive",
        method="describe",
        path="",
        source=f"dubbo:{ENDPOINT}",
        asserts=[
            Assertion(kind="rpc_status", op="eq", expect="OK"),
            Assertion(kind="result_present", op="exists"),
            Assertion(
                kind="result_field", target="message", op="eq", expect="hello Ada"
            ),
        ],
        dubbo=DubboCall(
            interface="io.astsight.sidecar.dubbo.EchoService",
            method="describe",
            parameter_types=["io.astsight.sidecar.dubbo.EchoRequest"],
            arguments=[{"name": "Ada"}],
            group="order",
            version="1.0.0",
            target_port=target_port,
        ),
    )


def _dubbo_only_project(root: Path) -> Path:
    package = root / "src" / "main" / "java" / "io" / "astsight" / "sidecar" / "dubbo"
    package.mkdir(parents=True)
    (package / "EchoService.java").write_text(
        "package io.astsight.sidecar.dubbo;\n"
        "public interface EchoService {\n"
        "    EchoReply describe(EchoRequest request);\n"
        "}\n",
        encoding="utf-8",
    )
    (package / "EchoRequest.java").write_text(
        "package io.astsight.sidecar.dubbo;\n"
        "public class EchoRequest {\n"
        "    private String name;\n"
        "    public String getName() { return name; }\n"
        "    public void setName(String name) { this.name = name; }\n"
        "}\n",
        encoding="utf-8",
    )
    (package / "EchoReply.java").write_text(
        "package io.astsight.sidecar.dubbo;\n"
        "public class EchoReply {\n"
        "    private String message;\n"
        "    public String getMessage() { return message; }\n"
        "    public void setMessage(String message) { this.message = message; }\n"
        "}\n",
        encoding="utf-8",
    )
    (package / "EchoServiceImpl.java").write_text(
        "package io.astsight.sidecar.dubbo;\n"
        "import org.apache.dubbo.config.annotation.DubboService;\n"
        "@DubboService(group = \"order\", version = \"1.0.0\")\n"
        "public class EchoServiceImpl implements EchoService {\n"
        "    public EchoReply describe(EchoRequest request) { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    return root


@pytest.mark.skipif(
    not JAR.is_file() or not CLASSPATH_FILE.is_file(),
    reason="先构建 java/astsight-sidecar-dubbo",
)
def test_run_generates_and_executes_dubbo_cases_in_full_mode(
    tmp_path: Path,
) -> None:
    java = locate_java()
    port = _free_port()
    project = _dubbo_only_project(tmp_path / "project")
    provider = _start_provider(java, port)
    try:
        report = run(
            RunConfig(
                project=str(project),
                mode="full",
                out=str(tmp_path / "runs"),
                codegraph="off",
                dubbo_url=f"dubbo://127.0.0.1:{port}",
                min_pass_rate=0.0,
            )
        )
    finally:
        if provider.stdin is not None:
            try:
                provider.stdin.close()
            except OSError:
                pass
        try:
            provider.wait(timeout=10)
        except subprocess.TimeoutExpired:
            provider.kill()
            provider.wait(timeout=10)

    assert report["summary"]["executed_cases"] == 1
    assert report["summary"]["final_pass"] == 1
    assert report["gate"]["verdict"] == "pass"


@pytest.mark.skipif(
    not JAR.is_file() or not CLASSPATH_FILE.is_file(),
    reason="先构建 java/astsight-sidecar-dubbo",
)
def test_generated_dubbo_case_runs_against_real_provider(
    tmp_path: Path,
) -> None:
    java = locate_java()
    port = _free_port()
    provider = _start_provider(java, port)
    try:
        generated = tmp_path / "generated"
        codegen = generate_pytest([_case(target_port=port)], generated)
        assert codegen.errors == [], codegen.errors

        # 不传完整 URL，只给 host，验证画像端口 + ASTSIGHT_DUBBO_HOST 的解析。
        execution = run_pytest(
            generated,
            dubbo_host="127.0.0.1",
            timeout=120,
        )

        assert execution.returncode == 0, (
            execution.stdout[-4000:] + "\n" + execution.stderr[-4000:]
        )
        assert len(execution.records) == 1
        assert execution.records[0].outcome == "passed"
        assert execution.records[0].case_id == "TC-0001"
    finally:
        if provider.stdin is not None:
            try:
                provider.stdin.close()
            except OSError:
                pass
        try:
            provider.wait(timeout=10)
        except subprocess.TimeoutExpired:
            provider.kill()
            provider.wait(timeout=10)
