"""MQ 生产者拦截链路：Agent 上报 → 会话证据 → 生成的 mq_tools 断言。

这里用一个进程内 SUT 模拟 Agent 的 `/_admin/mq/publish` 上报，不启动 Kafka，
验证的是 AstSight 侧的会话、证据与断言闭环。
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from astsight.codegen import generate_pytest
from astsight.execute.runner import run_pytest
from astsight.mock import MockHttpServer
from contracts import Assertion, MqAssertion, TestCase


def _case() -> TestCase:
    return TestCase(
        id="TC-MQ01",
        endpoint="GET /publish",
        category="business",
        method="GET",
        path="/publish",
        source="test",
        asserts=[Assertion(kind="status_code", op="eq", expect=200)],
        mq_assertions=[
            MqAssertion(
                topic="orders-topic",
                key="O-1001",
                headers={"trace": "t1"},
                body={"order_id": "O-1001", "status": "paid"},
                count=1,
            )
        ],
    )


class _SutHandler(BaseHTTPRequestHandler):
    admin_url: str
    token: str

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        payload = json.dumps(
            {
                "sut_id": "sut-mq",
                "client": "kafka",
                "topic": "orders-topic",
                "key": "O-1001",
                "headers": {"trace": "t1"},
                "body": {"order_id": "O-1001", "status": "paid"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.admin_url}/_admin/mq/publish",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            handled = json.loads(response.read().decode("utf-8"))
        body = json.dumps({"handled": handled.get("handled")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


def _serve_sut(admin_url: str, token: str):
    handler = type(
        "Handler", (_SutHandler,), {"admin_url": admin_url, "token": token}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_generated_mq_assertion_reads_intercepted_messages(
    tmp_path: Path, monkeypatch
) -> None:
    for name in ("ASTSIGHT_MOCK_ADMIN_URL", "ASTSIGHT_MOCK_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    mock = MockHttpServer(sut_id="sut-mq", token="mq-token")
    mock.add_mq_dep("mq:kafka:orders-topic", topic="orders-topic", client="kafka")
    mock.start()
    sut, base_url = _serve_sut(mock.admin_endpoint, mock.token)
    generated = tmp_path / "generated"
    try:
        result = generate_pytest([_case()], generated)
        assert not result.errors
        execution = run_pytest(
            generated,
            base_url=base_url,
            mock_admin_url=mock.admin_endpoint,
            mock_token=mock.token,
            mock_namespace="sut-mq",
        )
    finally:
        sut.shutdown()
        sut.server_close()
        mock.stop()

    assert execution.records[0].outcome == "passed", (
        execution.stdout + execution.stderr
    )
    calls = mock.evidence.records
    assert len(calls) == 1
    assert calls[0].transport == "mq"
    assert calls[0].adapter == "mq-producer"
    assert calls[0].dep_id == "mq:kafka:orders-topic"
