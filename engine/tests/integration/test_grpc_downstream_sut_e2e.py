"""真实 SUT → gRPC 下游 Mock 的端到端闭环。"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest
from grpc_tools import protoc

from astsight.agent.sut import start_sut
from astsight.mock import start_mock_plan

GRIPMOCK_BIN = os.environ.get("GRIPMOCK_BIN", "").strip()
pytestmark = pytest.mark.skipif(
    not GRIPMOCK_BIN or not Path(GRIPMOCK_BIN).is_file(),
    reason="set GRIPMOCK_BIN to a real gripmock binary",
)

PROTO = """
syntax = "proto3";
package inventory.v1;

message ReserveRequest { string sku = 1; }
message ReserveResponse {
  bool available = 1;
  string reservation_id = 2;
}

service InventoryService {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
"""

SUT = """
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
from inventory_pb2 import ReserveRequest
from inventory_pb2_grpc import InventoryServiceStub

channel = grpc.insecure_channel(os.environ["INVENTORY_GRPC_ADDR"])
stub = InventoryServiceStub(channel)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        try:
            response = stub.Reserve(ReserveRequest(sku="SKU-1"))
            status = 200
            payload = {
                "available": response.available,
                "reservation_id": response.reservation_id,
            }
        except grpc.RpcError as exc:
            status = 502
            payload = {
                "code": exc.code().name,
                "details": exc.details(),
            }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["SUT_PORT"])), Handler)
server.serve_forever()
"""

GRPC_PATH = "/inventory.v1.InventoryService/Reserve"
DEP_ID = f"grpc:{GRPC_PATH}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_project(tmp_path: Path) -> tuple[Path, Path]:
    proto = tmp_path / "inventory.proto"
    proto.write_text(PROTO, encoding="utf-8")
    assert (
        protoc.main(
            [
                "protoc",
                f"-I{tmp_path}",
                f"--python_out={tmp_path}",
                f"--grpc_python_out={tmp_path}",
                str(proto),
            ]
        )
        == 0
    )
    sut = tmp_path / "sut.py"
    sut.write_text(SUT, encoding="utf-8")
    plan = {
        "sut_id": "grpc-sut-e2e",
        "deps": [
            {
                "dep_id": DEP_ID,
                "transport": "grpc",
                "locator": {
                    "transport": "grpc",
                    "grpc_path": GRPC_PATH,
                    "descriptor_source": "inventory.proto",
                    "property": "INVENTORY_GRPC_ADDR",
                },
                "branches": {
                    "success": {
                        "body": {
                            "available": True,
                            "reservation_id": "R-1",
                        }
                    },
                    "unavailable": {"body": None},
                },
                "replies": {
                    "success": {
                        "transport": "grpc",
                        "grpc_status": "OK",
                        "body": {
                            "available": True,
                            "reservation_id": "R-1",
                        },
                    },
                    "unavailable": {
                        "transport": "grpc",
                        "grpc_status": "UNAVAILABLE",
                        "message": "inventory offline",
                    },
                },
            }
        ],
    }
    plan_path = tmp_path / "grpc-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path, sut


def _open_session(runtime, session_id: str, branch: str) -> dict:
    headers = {"Authorization": f"Bearer {runtime.token}"}
    response = httpx.post(
        f"{runtime.server.endpoint}/_admin/sessions",
        headers=headers,
        json={
            "session_id": session_id,
            "namespace": "grpc-sut-e2e",
            "case_id": session_id,
            "bindings": [{"dep_id": DEP_ID, "branch": branch}],
        },
        timeout=5,
    )
    assert response.status_code == 200, response.text
    return headers


def _close_session(runtime, headers: dict, session_id: str) -> None:
    drain = httpx.post(
        f"{runtime.server.endpoint}/_admin/sessions/{session_id}/drain",
        headers=headers,
        json={"timeout_ms": 500},
        timeout=5,
    )
    assert drain.status_code == 200, drain.text
    deleted = httpx.delete(
        f"{runtime.server.endpoint}/_admin/sessions/{session_id}",
        headers=headers,
        timeout=5,
    )
    assert deleted.status_code == 200, deleted.text


def test_real_sut_calls_gripmock_success_and_error(tmp_path: Path) -> None:
    plan_path, sut_path = _write_project(tmp_path)
    runtime = start_mock_plan(
        plan_path,
        history_path=tmp_path / "mock_calls.jsonl",
        gripmock_path=GRIPMOCK_BIN,
        grpc_start_timeout_s=20,
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    sut = None
    try:
        sut = start_sut(
            [sys.executable, str(sut_path)],
            base_url=base_url,
            log_path=tmp_path / "sut.log",
            injection_document=runtime.injection_document(),
            extra_env={"SUT_PORT": str(port)},
            ready_url=f"{base_url}/health",
            timeout=20,
            cwd=tmp_path,
        )

        headers = _open_session(runtime, "TC-success", "success")
        success = httpx.get(base_url, timeout=5)
        assert success.status_code == 200, success.text
        assert success.json() == {
            "available": True,
            "reservation_id": "R-1",
        }
        _close_session(runtime, headers, "TC-success")

        headers = _open_session(runtime, "TC-error", "unavailable")
        error = httpx.get(base_url, timeout=5)
        assert error.status_code == 502, error.text
        assert error.json()["code"] == "UNAVAILABLE"
        assert error.json()["details"] == "inventory offline"
        _close_session(runtime, headers, "TC-error")
    finally:
        if sut is not None:
            sut.stop()
        runtime.stop()

    records = [
        json.loads(line)
        for line in runtime.server.evidence.path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert [item["branch"] for item in records] == ["success", "unavailable"]
    assert [item["grpc_status"] for item in records] == ["OK", "UNAVAILABLE"]
