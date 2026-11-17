"""真实 sidecar jar：进程启动、ping 自检、无 provider 的错误归一。

启用条件：

- `ASTSIGHT_DUBBO_SIDECAR_E2E=1`
- 已构建 `java/astsight-sidecar-dubbo/target/astsight-dubbo-sidecar.jar`
- `JAVA_HOME` / `ASTSIGHT_JAVA` / PATH 中有 JDK 8+

这里不启动 provider；真实泛化调用与 POJO/附件由 Java 单测
`DubboInvokerTest` 覆盖，本用例只验证 Python ↔ Java 进程边界。
"""
from __future__ import annotations

import os
import socket

import pytest

from astsight.sidecar import (
    SIDECAR_PROTOCOL,
    DubboSidecar,
    DubboSidecarError,
    locate_sidecar_jar,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_DUBBO_SIDECAR_E2E") != "1",
    reason="Dubbo sidecar 联调默认跳过：设置 ASTSIGHT_DUBBO_SIDECAR_E2E=1 启用",
)


def _closed_port() -> int:
    socket_handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        socket_handle.bind(("127.0.0.1", 0))
        return socket_handle.getsockname()[1]
    finally:
        socket_handle.close()


def test_sidecar_starts_and_reports_protocol() -> None:
    jar = locate_sidecar_jar()
    with DubboSidecar(jar=jar) as sidecar:
        info = sidecar.info
        assert info["protocol"] == SIDECAR_PROTOCOL
        assert info["dubbo_version"]
        assert info["java_version"]


def test_bad_request_does_not_start_a_reference() -> None:
    with DubboSidecar() as sidecar:
        result = sidecar.invoke(
            "com.acme.OrderService",
            "get",
            [],
            url="",
        )
    assert not result.ok
    assert result.error == "direct_url_required"
    assert result.rpc_status == "BAD_REQUEST"


def test_no_provider_is_a_structured_failure() -> None:
    port = _closed_port()
    with DubboSidecar() as sidecar:
        result = sidecar.invoke(
            "com.acme.OrderService",
            "get",
            [],
            url=f"dubbo://127.0.0.1:{port}",
            timeout_ms=1_000,
        )
    assert not result.ok
    assert result.rpc_status in {"NETWORK", "NO_PROVIDER", "RPC_EXCEPTION"}
    assert result.error in {"network_error", "no_provider", "rpc_exception"}


def test_java_toolchain_missing_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAVA_HOME", "")
    monkeypatch.setenv("ASTSIGHT_JAVA", "")
    monkeypatch.setenv("PATH", "")
    sidecar = DubboSidecar(jar=locate_sidecar_jar())
    with pytest.raises(DubboSidecarError) as error:
        sidecar.start()
    assert error.value.code == "sidecar_java_missing"
