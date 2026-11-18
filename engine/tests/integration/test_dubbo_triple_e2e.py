"""真实 Dubbo Triple provider → 画像 → codegen → gRPC-compatible unary。"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from astsight.codegen import generate_pytest
from astsight.profile.dubbo import scan_dubbo_sources
from astsight.pipeline import RunConfig, run
from astsight.sidecar import locate_java
from contracts import Assertion, DubboCall, TestCase, Transport

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "java" / "samples" / "dubbo-triple-provider"
JAR = SAMPLE / "target" / "dubbo-triple-provider.jar"

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_DUBBO_TRIPLE_E2E") != "1",
    reason="Triple 样例联调默认跳过：设置 ASTSIGHT_DUBBO_TRIPLE_E2E=1 启用",
)


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


def _provider_profile() -> dict:
    files = [
        (path, "")
        for path in sorted(SAMPLE.rglob("*"))
        if path.is_file() and "target" not in path.parts
    ]
    scan = scan_dubbo_sources(
        files=files,
        root=SAMPLE,
        read_text=lambda path: path.read_text(encoding="utf-8"),
    )
    assert len(scan.providers) == 1, scan.unsupported
    return scan.providers[0]


@pytest.mark.skipif(
    not JAR.is_file(),
    reason="先构建 java/samples/dubbo-triple-provider",
)
def test_real_triple_provider_generated_call(tmp_path: Path) -> None:
    profile = _provider_profile()
    assert profile["protocol"] == "tri"
    assert profile["serialization"] == "protobuf"
    assert profile["compatibility"] == "grpc_compatible"
    assert profile["descriptor_source"] == "src/main/proto/inventory.proto"
    assert profile["grpc_path"] == "/io.astsight.triple.InventoryService/Reserve"

    port = _free_port()
    stdout = (tmp_path / "provider.stdout.log").open("w", encoding="utf-8")
    stderr = (tmp_path / "provider.stderr.log").open("w", encoding="utf-8")
    creationflags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0
    )
    provider = subprocess.Popen(
        [locate_java(), "-jar", str(JAR), str(port)],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        _wait_port(port, provider)
        call = DubboCall(
            interface=profile["service"],
            method=profile["method"],
            parameter_types=list(profile["parameter_types"]),
            arguments=[{"sku": "SKU-1"}],
            url=f"tri://127.0.0.1:{port}",
            group=profile["group"],
            version=profile["version"],
            serialization=profile["serialization"],
            descriptor_source=str(SAMPLE / profile["descriptor_source"]),
            grpc_path=profile["grpc_path"],
            compatibility=profile["compatibility"],
        )
        case = TestCase(
            id="TC-TRIPLE-1",
            transport=Transport.DUBBO,
            endpoint=profile["endpoint"],
            category="positive",
            method=call.method,
            path="",
            source="dubbo:triple-e2e",
            asserts=[
                Assertion(kind="rpc_status", op="eq", expect="OK"),
                Assertion(
                    kind="result_field",
                    target="available",
                    op="eq",
                    expect=True,
                ),
                Assertion(
                    kind="result_field",
                    target="reservation_id",
                    op="eq",
                    expect="TRIPLE-SKU-1",
                ),
            ],
            dubbo=call,
        )
        generated = tmp_path / "generated"
        result = generate_pytest([case], generated)
        assert result.errors == []
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "engine"), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(generated)],
            cwd=SAMPLE,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=creationflags,
        )
        assert completed.returncode == 0, (
            completed.stdout + "\n" + completed.stderr
        )
        project = tmp_path / "triple_project"
        shutil.copytree(SAMPLE / "src", project / "src")
        report = run(
            RunConfig(
                project=str(project),
                mode="full",
                out=str(tmp_path / "runs"),
                codegraph="off",
                dubbo_url=f"tri://127.0.0.1:{port}",
                min_pass_rate=0.0,
            )
        )
        assert report["summary"]["executed_cases"] >= 1
        assert report["summary"]["final_pass"] >= 1, report.get("failures")
        assert report["gate"]["verdict"] == "pass", report["gate"]
    finally:
        provider.terminate()
        try:
            provider.wait(timeout=10)
        except subprocess.TimeoutExpired:
            provider.kill()
            provider.wait(timeout=10)
        stdout.close()
        stderr.close()
