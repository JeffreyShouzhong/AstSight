"""Python SUT 的 MQ 采集注入环境。"""
from __future__ import annotations

from pathlib import Path

from astsight.agent.sut import _is_python_command, _mq_capture_env


def _document() -> dict:
    return {
        "sut_id": "sut-py",
        "endpoint": "http://127.0.0.1:9000",
        "mq_deps": [{"dep_id": "mq:kafka:orders", "topic": "orders"}],
    }


def test_python_sut_gets_mq_capture_env() -> None:
    env: dict[str, str] = {}
    keys: list[str] = []
    warnings: list[str] = []
    _mq_capture_env(
        env,
        keys,
        argv=["python", "app.py"],
        document=_document(),
        agent_token="secret",
        warnings=warnings,
    )
    assert warnings == []
    assert env["ASTSIGHT_MQ_CAPTURE"] == "1"
    assert env["ASTSIGHT_MQ_ADMIN_URL"] == "http://127.0.0.1:9000"
    assert env["ASTSIGHT_MQ_TOKEN"] == "secret"
    assert env["ASTSIGHT_MQ_SUT_ID"] == "sut-py"
    assert env["PYTHONPATH"].endswith("astsight" + str(Path("/mq/inject")))
    assert "PYTHONPATH" in keys


def test_java_sut_is_not_injected() -> None:
    env: dict[str, str] = {}
    keys: list[str] = []
    warnings: list[str] = []
    _mq_capture_env(
        env,
        keys,
        argv=["java", "-jar", "app.jar"],
        document=_document(),
        agent_token="secret",
        warnings=warnings,
    )
    assert env == {}
    assert keys == []
    assert warnings == []


def test_python_command_detection() -> None:
    assert _is_python_command(["python", "app.py"])
    assert _is_python_command(["uvicorn", "app:app"])
    assert not _is_python_command(["java", "-jar", "app.jar"])
