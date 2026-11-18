"""`agent run` 全链路：自动 gRPC 画像 → semantic → plan → SUT → report。"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from grpc_tools import protoc

from astsight.agent.flow import FlowConfig, run_flow
from astsight.cases.context import build_context
from astsight.inventory import inventory_routes
from astsight.rules.validate import rule_key

GRIPMOCK_BIN = os.environ.get("GRIPMOCK_BIN", "").strip()
pytestmark = pytest.mark.skipif(
    not GRIPMOCK_BIN or not Path(GRIPMOCK_BIN).is_file(),
    reason="set GRIPMOCK_BIN to a real gripmock binary",
)

DOC = "docs/requirements.md"
ENDPOINT = "GET /orders"
RULE_TEXT = "订单接口成功时返回库存可用状态和预约号"
DEP_ID = "grpc:/inventory.v1.InventoryService/Reserve"

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

APP = """
import os
from fastapi import FastAPI
import grpc
from inventory_pb2 import ReserveRequest
from inventory_pb2_grpc import InventoryServiceStub

app = FastAPI()
channel = grpc.insecure_channel(os.environ["INVENTORY_GRPC_ADDR"])
stub = InventoryServiceStub(channel)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/orders")
def orders():
    result = stub.Reserve(ReserveRequest(sku="SKU-1"))
    return {
        "available": result.available,
        "reservation_id": result.reservation_id,
    }
"""

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "gRPC SUT", "version": "1.0.0"},
    "paths": {
        "/health": {
            "get": {
                "operationId": "health",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/orders": {
            "get": {
                "operationId": "orders",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "available": {"type": "boolean"},
                                        "reservation_id": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
    },
}


class _Usage:
    def as_dict(self) -> dict:
        return {"prompt_chars": 10, "attempts": 1}


class ScriptedLLM:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls: list[dict[str, str]] = []
        self.config = SimpleNamespace(
            redacted=lambda: {"provider": "fake", "model": "fake"}
        )
        self.usage = [_Usage()]

    def complete_json(self, *, system: str, user: str):
        self.calls.append({"system": system, "user": user})
        return self.payloads.pop(0)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "grpc_sut"
    (root / "docs").mkdir(parents=True)
    (root / DOC).write_text(
        f"# gRPC SUT\n\n## {ENDPOINT}\n\n- {RULE_TEXT}。\n",
        encoding="utf-8",
    )
    (root / "inventory.proto").write_text(PROTO, encoding="utf-8")
    assert (
        protoc.main(
            [
                "protoc",
                f"-I{root}",
                f"--python_out={root}",
                f"--grpc_python_out={root}",
                str(root / "inventory.proto"),
            ]
        )
        == 0
    )
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "openapi.json").write_text(
        json.dumps(OPENAPI),
        encoding="utf-8",
    )
    return root


def _section(project: Path) -> str:
    routes, _audit, _graph, _notes = inventory_routes(
        project,
        codegraph="off",
        openapi_source=str(project / "openapi.json"),
    )
    context = build_context(
        project,
        [DOC],
        openapi=str(project / "openapi.json"),
        routes=routes,
    )
    for document in context["documents"]:
        for section in document["sections"]:
            if section["section"].endswith(ENDPOINT):
                return section["section"]
    raise AssertionError("section not found")


def _rules(section: str) -> dict:
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "summary": "订单接口依赖库存 gRPC",
        "rules": [
            {
                "text": RULE_TEXT,
                "type": "other",
                "entities": ["订单"],
                "fields": ["available", "reservation_id"],
                "source": {
                    "doc": DOC,
                    "section": section,
                    "quote": RULE_TEXT,
                    "kind": "explicit",
                },
                "authority": "A",
                "impact": "P1-功能",
                "priority": "P1",
                "rule_id": "BR-01",
            }
        ],
        "questions": [],
        "gaps": [],
        "conflicts": [],
    }


def _draft(section: str, key: str) -> dict:
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "cases": [
            {
                "case_key": "order_grpc_success",
                "title": "订单接口返回 gRPC 库存桩",
                "level": "business",
                "requirement_refs": [
                    {"doc": DOC, "section": section, "quote": RULE_TEXT}
                ],
                "rule_refs": [{"rule_key": key, "rule_id": "BR-01"}],
                "endpoint": ENDPOINT,
                "category": "positive",
                "asserts": [
                    {"kind": "status_code", "op": "eq", "expect": 200},
                    {
                        "kind": "body_field",
                        "target": "available",
                        "op": "eq",
                        "expect": False,
                    },
                    {
                        "kind": "body_field",
                        "target": "reservation_id",
                        "op": "eq",
                        "expect": "",
                    },
                ],
                "mock_bindings": {DEP_ID: "success"},
                "rationale": "库存服务返回成功桩",
            }
        ],
    }


def test_agent_run_auto_grpc_plan_and_execution(tmp_path: Path) -> None:
    project = _project(tmp_path)
    section = _section(project)
    key = rule_key(DOC, section, RULE_TEXT)
    port = _free_port()
    config = FlowConfig(
        project=str(project),
        requirements=[DOC],
        openapi=str(project / "openapi.json"),
        mode="full",
        codegraph="off",
        base_url=f"http://127.0.0.1:{port}",
        sut_command=[
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        sut_ready_url=f"http://127.0.0.1:{port}/health",
        approve_as="e2e",
        out=".astsight/agent",
        timeout=180.0,
        sut_timeout=30.0,
        allow_unmapped=True,
        skip_data=True,
        gripmock_path=GRIPMOCK_BIN,
    )
    result = run_flow(
        config,
        llm=ScriptedLLM([_rules(section), _draft(section, key)]),
    )

    assert result.status == "pass", (result.steps, result.warnings, result.report)
    assert result.report is not None
    assert result.report["gate"]["verdict"] == "pass"
    run_dir = Path(result.run_dir)
    plan = json.loads((run_dir / "mock_plan.json").read_text("utf-8"))
    assert plan["deps"][0]["dep_id"] == DEP_ID
    assert plan["deps"][0]["locator"]["descriptor_source"] == str(
        (project / "inventory.proto").resolve()
    )
    assert plan["deps"][0]["replies"]["success"]["transport"] == "grpc"
    injection = json.loads(
        (run_dir / "mock_injection.json").read_text("utf-8")
    )
    assert injection["injections"][0]["strategy"] == "grpc_server"
    evidence = json.loads(
        (run_dir / "mock_evidence.json").read_text("utf-8")
    )
    assert evidence["calls"] == 1
    assert evidence["by_result"] == {"ok": 1}
    manifest = json.loads(
        (run_dir / "run_manifest.json").read_text("utf-8")
    )
    assert manifest["grpc_endpoint"]
    assert manifest["grpc_binary"]["source"] == "override"
    assert manifest["grpc_binary"]["degraded"] is True
    calls = [
        json.loads(line)
        for line in (run_dir / "mock_calls.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1
    assert calls[0]["transport"] == "grpc"
    assert calls[0]["dep_id"] == DEP_ID
    assert calls[0]["branch"] == "success"
