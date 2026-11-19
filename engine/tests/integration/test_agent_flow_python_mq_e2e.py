"""`agent run` + Python SUT：自动注入 MQ 采集并执行消息断言。

默认跳过。启用条件：

- `ASTSIGHT_MQ_E2E=1`
- 当前解释器可导入 `kafka`（kafka-python）

SUT 是真实 FastAPI + KafkaProducer，但不连 broker；依赖 `start_sut` 自动把
`astsight/mq/inject` 加进 `PYTHONPATH`，再由 `sitecustomize` 拦截 send。
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astsight.agent.flow import FlowConfig, run_flow
from astsight.cases.context import build_context
from astsight.inventory import inventory_routes
from astsight.rules.validate import rule_key

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_MQ_E2E") != "1"
    or importlib.util.find_spec("kafka") is None,
    reason="Python MQ agent e2e 默认跳过：设置 ASTSIGHT_MQ_E2E=1 且安装 kafka-python",
)

DOC = "docs/requirements.md"
TEXT = "发布订单事件时必须发送订单消息"
TOPIC = "orders-topic"

_APP = """
from fastapi import FastAPI
from kafka import KafkaProducer

app = FastAPI()
TOPIC = "orders-topic"
producer = KafkaProducer(
    bootstrap_servers="127.0.0.1:1",
    api_version=(2, 0, 0),
)

@app.get("/publish")
def publish():
    producer.send(TOPIC, value=b'{"order_id":"O-1001"}', key=b"K-1")
    return {"ok": True}
"""

_OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "mq", "version": "1.0"},
    "paths": {
        "/publish": {
            "get": {"responses": {"200": {"description": "ok"}}}
        }
    },
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "mq_service"
    (project / "docs").mkdir(parents=True)
    (project / "app.py").write_text(_APP, encoding="utf-8")
    (project / "openapi.json").write_text(
        json.dumps(_OPENAPI), encoding="utf-8"
    )
    (project / DOC).write_text(
        f"# 发布\n\n## GET /publish\n\n{TEXT}。\n", encoding="utf-8"
    )
    return project


def _section(project: Path) -> str:
    routes, _audit, _graph, _notes = inventory_routes(
        project, codegraph="off", openapi_source=str(project / "openapi.json")
    )
    context = build_context(
        project,
        [DOC],
        openapi=str(project / "openapi.json"),
        routes=routes,
    )
    for document in context["documents"]:
        for section in document["sections"]:
            if section["section"].endswith("GET /publish"):
                return section["section"]
    raise AssertionError("section not found")


def _rules(section: str) -> dict:
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "summary": "发布接口发送订单消息",
        "rules": [
            {
                "text": TEXT,
                "type": "other",
                "entities": ["订单"],
                "fields": ["order_id"],
                "source": {
                    "doc": DOC,
                    "section": section,
                    "quote": TEXT,
                    "kind": "explicit",
                },
                "authority": "A",
                "impact": "P1-功能",
                "priority": "P1",
            }
        ],
        "questions": [],
        "gaps": [],
        "conflicts": [],
    }


def _draft(section: str) -> dict:
    key = rule_key(DOC, section, TEXT)
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "cases": [
            {
                "case_key": "publish_order_event",
                "title": "发布订单事件",
                "level": "business",
                "requirement_refs": [
                    {"doc": DOC, "section": section, "quote": TEXT}
                ],
                "rule_refs": [
                    {
                        "rule_key": key,
                        "rule_id": "BR-01",
                        "authority": "A",
                        "source_kind": "explicit",
                    }
                ],
                "endpoint": "GET /publish",
                "category": "positive",
                "asserts": [
                    {"kind": "status_code", "op": "eq", "expect": 200}
                ],
                "mq_assertions": [
                    {
                        "client": "kafka",
                        "topic": TOPIC,
                        "key": "K-1",
                        "body": {"order_id": "O-1001"},
                        "count": 1,
                    }
                ],
                "mock_bindings": {},
                "rationale": "规则要求发布订单消息",
            }
        ],
    }


class _Usage:
    def as_dict(self) -> dict:
        return {"prompt_chars": 10, "attempts": 1}


class ScriptedLLM:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.calls: list[dict[str, str]] = []
        self.config = SimpleNamespace(
            redacted=lambda: {"provider": "fake", "model": "fake"}
        )
        self.usage = [_Usage()]

    def complete_json(self, *, system: str, user: str):
        self.calls.append({"system": system, "user": user})
        return self.payloads.pop(0)


def _config(project: Path, port: int) -> FlowConfig:
    return FlowConfig(
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
        approve_as="tester",
        out=".astsight/agent",
        timeout=180.0,
        sut_timeout=30.0,
        allow_unmapped=True,
        max_repair_rounds=0,
    )


def test_agent_run_auto_injects_python_mq_capture(tmp_path: Path) -> None:
    project = _project(tmp_path)
    section = _section(project)
    llm = ScriptedLLM(
        [
            _rules(section),
            _draft(section),
            {"schema_version": 2, "diagnoses": []},
        ]
    )

    result = run_flow(_config(project, _free_port()), llm=llm)

    assert result.status == "pass", (result.steps, result.warnings, result.report)
    assert result.report["gate"]["verdict"] == "pass"
    execution = json.loads(
        (Path(result.run_dir) / "execution.json").read_text(encoding="utf-8")
    )
    assert [item["outcome"] for item in execution["records"]] == ["passed"]
    evidence = json.loads(
        (Path(result.run_dir) / "mock_evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["calls"] == 1
    heartbeat = result.report["mock"]["agent"]["heartbeat"]
    assert heartbeat["mq"]["installed"] is True
    assert "kafka" in heartbeat["mq"]["clients"]
    assert result.report["mock"]["plan"]["mq_route_count"] == 1
    assert result.report["mock"]["plan"]["mq_clients"] == ["kafka"]
