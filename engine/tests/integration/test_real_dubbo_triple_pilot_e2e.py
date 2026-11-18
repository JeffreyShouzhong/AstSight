"""真实 Apache Online Boutique Triple 项目试点。

环境变量：

- ``ASTSIGHT_REAL_TRIPLE_E2E=1``
- ``ASTSIGHT_REAL_TRIPLE_PROJECT=<online_bontique_demo 根目录>``
- 可选 ``ASTSIGHT_REAL_TRIPLE_JAR``，默认使用 adService target fat jar
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from astsight.pipeline import RunConfig, run
from astsight.sidecar import locate_java, locate_sidecar_jar

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_REAL_TRIPLE_E2E") != "1",
    reason="设置 ASTSIGHT_REAL_TRIPLE_E2E=1 启用真实 Triple 试点",
)


def _project() -> Path:
    raw = os.environ.get("ASTSIGHT_REAL_TRIPLE_PROJECT", "").strip()
    if not raw:
        pytest.skip("设置 ASTSIGHT_REAL_TRIPLE_PROJECT")
    path = Path(raw).resolve()
    if not path.is_dir():
        pytest.skip(f"Triple 项目不存在: {path}")
    return path


def _jar(project: Path) -> Path:
    configured = os.environ.get("ASTSIGHT_REAL_TRIPLE_JAR", "").strip()
    path = (
        Path(configured).resolve()
        if configured
        else project / "adService" / "target" / "adService-1.0-SNAPSHOT.jar"
    )
    if not path.is_file():
        pytest.skip(f"先构建 Triple provider: {path}")
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, process: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Triple provider 提前退出: {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError("Triple provider 启动超时")


def test_online_boutique_triple_full_run(tmp_path: Path) -> None:
    project = _project()
    jar = _jar(project)
    locate_sidecar_jar()
    port = _free_port()
    stdout = (tmp_path / "provider.stdout.log").open("w", encoding="utf-8")
    stderr = (tmp_path / "provider.stderr.log").open("w", encoding="utf-8")
    creationflags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0
    )
    provider = subprocess.Popen(
        [
            locate_java(),
            "-jar",
            str(jar),
            "--dubbo.registry.address=N/A",
            "--dubbo.protocol.name=tri",
            f"--dubbo.protocol.port={port}",
            "--dubbo.application.qos-enable=false",
        ],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    out = tmp_path / "runs"
    try:
        _wait_port(port, provider)
        report = run(
            RunConfig(
                project=str(project),
                mode="full",
                out=str(out),
                codegraph="off",
                dubbo_url=f"tri://127.0.0.1:{port}",
                min_pass_rate=0.0,
                case_base=str(tmp_path / "case-base"),
            )
        )
    finally:
        provider.terminate()
        try:
            provider.wait(timeout=10)
        except subprocess.TimeoutExpired:
            provider.kill()
            provider.wait(timeout=10)
        stdout.close()
        stderr.close()

    assert report["summary"]["executed_cases"] == 1
    assert report["summary"]["final_pass"] == 1, report.get("blocked")
    assert report["gate"]["verdict"] == "pass"
    run_dir = out / report["run"]["run_id"]
    profile = json.loads((run_dir / "profiles.json").read_text("utf-8"))[
        "profiles"
    ][0]
    assert profile["protocol"] == "tri"
    assert profile["serialization"] == "hessian2"
    assert profile["compatibility"] == "native"
    assert profile["descriptor_source"] is None
    case = json.loads((run_dir / "cases.json").read_text("utf-8"))[0]
    assert case["dubbo"]["serialization"] == "hessian2"
    assert case["dubbo"]["arguments"] == [{"contextKeys": ["sample"]}]
