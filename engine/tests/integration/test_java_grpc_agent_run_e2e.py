"""真实 Java SUT 的 gRPC `agent run` 全链路。

启用：
1. 设置 ``GRIPMOCK_BIN``；
2. 构建 ``java/samples/spring-boot-grpc``。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from astsight.agent.flow import FlowConfig, run_flow
from astsight.cases.context import build_context
from astsight.inventory import inventory_routes
from astsight.rules.validate import rule_key

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "java" / "samples" / "spring-boot-grpc"
GRIPMOCK_BIN = os.environ.get("GRIPMOCK_BIN", "").strip()
DOC = "docs/requirements.md"
ENDPOINT = "GET /orders"
RULE_TEXT = "订单接口成功时返回库存可用状态和预约号"
DEP_ID = "grpc:/inventory.v1.InventoryService/Reserve"

pytestmark = pytest.mark.skipif(
    not GRIPMOCK_BIN or not Path(GRIPMOCK_BIN).is_file(),
    reason="set GRIPMOCK_BIN to a real gripmock binary",
)


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


def _java() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("ASTSIGHT_JAVA_AGENT_E2E_JAVA", "").strip()
    if configured:
        candidate = Path(configured)
        candidates.append(
            candidate / "bin" / "java" if candidate.is_dir() else candidate
        )
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        candidates.append(Path(home) / "bin" / "java")
    found = shutil.which("java")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
        executable = Path(str(candidate) + ".exe")
        if executable.is_file():
            return executable
    pytest.skip("未找到 java；设置 ASTSIGHT_JAVA_AGENT_E2E_JAVA 或 JAVA_HOME")


def _sample_jar() -> Path:
    jars = [
        path
        for path in (SAMPLE / "target").glob("spring-boot-grpc-sample-*.jar")
        if not path.name.endswith(".original")
    ]
    if not jars:
        pytest.skip(
            "Java gRPC 样例未构建：cd java/samples/spring-boot-grpc "
            "&& mvn -q -DskipTests package"
        )
    return sorted(jars)[0]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "java_grpc_sut"
    shutil.copytree(SAMPLE / "src", project / "src")
    shutil.copy2(SAMPLE / "pom.xml", project / "pom.xml")
    shutil.copy2(SAMPLE / "openapi.json", project / "openapi.json")
    docs = project / "docs"
    docs.mkdir(parents=True)
    (docs / "requirements.md").write_text(
        f"# Java gRPC SUT\n\n## {ENDPOINT}\n\n- {RULE_TEXT}。\n",
        encoding="utf-8",
    )
    return project


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
        "summary": "Java 订单接口依赖库存 gRPC",
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
                "case_key": "java_order_grpc_success",
                "title": "Java 订单接口返回 gRPC 库存桩",
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
                "rationale": "Java SUT 从 gripmock 取得库存成功桩",
            }
        ],
    }


def test_java_agent_run_auto_grpc_plan_and_execution(tmp_path: Path) -> None:
    project = _project(tmp_path)
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
    profile = next(
        item for item in context["profiles"] if item["endpoint"] == ENDPOINT
    )
    dep = next(
        item for item in profile["downstream_deps"] if item["dep_id"] == DEP_ID
    )
    assert dep["property"] == "INVENTORY_GRPC_ADDR"
    assert dep["languages"] == ["java"]
    assert any(
        site["stub"] == "InventoryServiceStub"
        for site in dep["call_sites"]
    )

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
            str(_java()),
            "-jar",
            str(_sample_jar()),
            f"--server.port={port}",
        ],
        sut_ready_url=f"http://127.0.0.1:{port}/health",
        approve_as="e2e",
        out=".astsight/agent",
        timeout=180.0,
        sut_timeout=45.0,
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
    assert plan["deps"][0]["locator"]["property"] == "INVENTORY_GRPC_ADDR"
    assert plan["deps"][0]["locator"]["descriptor_source"] == str(
        (project / "src" / "main" / "proto" / "inventory.proto").resolve()
    )
    injection = json.loads(
        (run_dir / "mock_injection.json").read_text("utf-8")
    )
    assert injection["injections"][0]["strategy"] == "grpc_server"
    assert injection["injections"][0]["property"] == "INVENTORY_GRPC_ADDR"
    assert injection["injections"][0]["config_key"] == "inventory.grpc.addr"
    assert injection["injections"][0]["config_kind"] == "spring_property"
    sut_log = (run_dir / "sut.log").read_text("utf-8", errors="replace")
    assert "-Dinventory.grpc.addr=127.0.0.1:" in sut_log
    evidence = json.loads(
        (run_dir / "mock_evidence.json").read_text("utf-8")
    )
    assert evidence["calls"] == 1
    assert evidence["by_result"] == {"ok": 1}
    calls = [
        json.loads(line)
        for line in (run_dir / "mock_calls.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1
    assert calls[0]["transport"] == "grpc"
    assert calls[0]["dep_id"] == DEP_ID
    assert calls[0]["branch"] == "success"
