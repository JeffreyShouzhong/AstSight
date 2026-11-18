"""常驻 Spring Boot Dubbo provider 样例的端到端试点。

启用条件：

- `ASTSIGHT_DUBBO_PROVIDER_SAMPLE_E2E=1`
- 已构建 `java/samples/dubbo-provider`：
  `cd java/samples/dubbo-provider && mvn -q -DskipTests package`
- JDK 17+

这个用例验证：真实 Spring Boot provider → AstSight 画像 → DTO 采样 →
codegen → sidecar 执行 → 报告。
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

from astsight.pipeline import RunConfig, run
from astsight.sidecar import locate_java

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "java" / "samples" / "dubbo-provider"
JAR = SAMPLE / "target" / "dubbo-provider-sample-0.1.0-SNAPSHOT.jar"

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_DUBBO_PROVIDER_SAMPLE_E2E") != "1",
    reason="常驻 provider 样例联调默认跳过：设置 "
    "ASTSIGHT_DUBBO_PROVIDER_SAMPLE_E2E=1 启用",
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


def _start(java: str, port: int) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            java,
            *_add_opens(java),
            "-jar",
            str(JAR),
            f"--dubbo.protocol.port={port}",
        ],
        stdin=subprocess.DEVNULL,
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

    deadline = time.monotonic() + 60
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
        "provider 样例未就绪：\n" + "\n".join(stderr_lines[-30:])
    )


@pytest.mark.skipif(
    not JAR.is_file(),
    reason="先构建 java/samples/dubbo-provider",
)
def test_provider_sample_full_run(tmp_path: Path) -> None:
    java = locate_java()
    port = _free_port()
    provider = _start(java, port)
    try:
        report = run(
            RunConfig(
                project=str(SAMPLE),
                mode="full",
                out=str(tmp_path / "runs"),
                codegraph="off",
                dubbo_url=f"dubbo://127.0.0.1:{port}",
                min_pass_rate=0.0,
            )
        )
    finally:
        provider.terminate()
        try:
            provider.wait(timeout=10)
        except subprocess.TimeoutExpired:
            provider.kill()
            provider.wait(timeout=10)

    assert report["summary"]["executed_cases"] == 5
    assert report["summary"]["final_pass"] == 5
    assert report["gate"]["verdict"] == "pass"
