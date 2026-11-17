"""Dubbo sidecar 客户端：进程生命周期、JSONL 协议与错误归一。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from astsight.sidecar import (
    SIDECAR_PROTOCOL,
    DubboSidecar,
    DubboSidecarError,
    locate_sidecar_jar,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_dubbo_sidecar.py"
INTERFACE = "com.acme.OrderService"


def _client() -> DubboSidecar:
    return DubboSidecar(
        command=[sys.executable, "-u", str(FIXTURE)],
        startup_timeout=5,
    )


def test_ping_reports_protocol_and_versions() -> None:
    with _client() as sidecar:
        assert sidecar.info["protocol"] == SIDECAR_PROTOCOL
        assert sidecar.info["sidecar_version"] == "test"
        assert sidecar.info["dubbo_version"] == "2.7.22"


def test_invoke_returns_value_and_passes_attachments() -> None:
    with _client() as sidecar:
        result = sidecar.invoke(
            INTERFACE,
            "create",
            ["java.lang.String"],
            ["O-1"],
            url="dubbo://127.0.0.1:20880",
            group="order",
            version="1.0.0",
            attachments={"tenant": "t-1"},
        )

    assert result.ok
    assert result.rpc_status == "OK"
    assert result.value == {
        "interface": INTERFACE,
        "method": "create",
        "arguments": ["O-1"],
        "attachments": {"tenant": "t-1"},
    }


def test_rpc_failure_is_returned_not_raised() -> None:
    with _client() as sidecar:
        result = sidecar.invoke(
            INTERFACE,
            "fail",
            [],
            url="dubbo://127.0.0.1:20880",
        )

    assert not result.ok
    assert result.error == "rpc_exception"
    assert result.rpc_status == "RPC_EXCEPTION"
    assert result.exception_type == "java.lang.IllegalStateException"
    assert result.message == "boom"


def test_raise_for_error_turns_result_into_exception() -> None:
    with _client() as sidecar:
        result = sidecar.invoke(
            INTERFACE,
            "fail",
            [],
            url="dubbo://127.0.0.1:20880",
        )

    with pytest.raises(DubboSidecarError) as error:
        result.raise_for_error()
    assert error.value.code == "rpc_exception"


def test_hanging_request_times_out() -> None:
    with _client() as sidecar:
        with pytest.raises(DubboSidecarError) as error:
            sidecar.invoke(
                INTERFACE,
                "hang",
                [],
                url="dubbo://127.0.0.1:20880",
                timeout=0.2,
            )
    assert error.value.code == "sidecar_timeout"


def test_process_exit_is_reported() -> None:
    with _client() as sidecar:
        with pytest.raises(DubboSidecarError) as error:
            sidecar.invoke(
                INTERFACE,
                "exit",
                [],
                url="dubbo://127.0.0.1:20880",
            )
    assert error.value.code == "sidecar_exited"


def test_non_json_stdout_is_a_protocol_error() -> None:
    with _client() as sidecar:
        with pytest.raises(DubboSidecarError) as error:
            sidecar.invoke(
                INTERFACE,
                "noisy",
                [],
                url="dubbo://127.0.0.1:20880",
            )
    assert error.value.code == "sidecar_protocol_error"


def test_missing_jar_fails_explicitly(tmp_path: Path) -> None:
    with pytest.raises(DubboSidecarError) as error:
        locate_sidecar_jar(tmp_path / "missing.jar")
    assert error.value.code == "sidecar_jar_missing"


def test_startup_protocol_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "sidecar.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    request=json.loads(line)\n"
        "    if request.get('op')=='ping':\n"
        "        print(json.dumps({'id':request.get('id'),'ok':True,'protocol':99}), flush=True)\n"
        "    elif request.get('op')=='shutdown':\n"
        "        print(json.dumps({'id':request.get('id'),'ok':True}), flush=True)\n"
        "        break\n",
        encoding="utf-8",
    )
    sidecar = DubboSidecar(
        command=[sys.executable, "-u", str(script)],
        startup_timeout=5,
    )
    with pytest.raises(DubboSidecarError) as error:
        sidecar.start()
    assert error.value.code == "sidecar_protocol_mismatch"
