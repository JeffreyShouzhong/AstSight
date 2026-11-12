"""Agent 端到端：真实 FastAPI SUT + 内嵌 Mock + 脚本化模型。

验证的不是“模型会不会写用例”，而是编排边界：
- 证据校验后才生成用例；
- 用例必须经 proposal/approve 才执行；
- Mock 先启动，注入环境变量后 SUT 才启动；
- 失败诊断与修复只作用于未批准草案，并通过新的 proposal 重跑。
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

from astsight.agent.flow import FlowConfig, run_flow
from astsight.cases.context import build_context
from astsight.inventory import inventory_routes
from astsight.rules.validate import rule_key

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "downstream_service"
DOC = "docs/requirements.md"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_example(tmp_path: Path) -> Path:
    target = tmp_path / "quote_service"
    shutil.copytree(EXAMPLE, target)
    return target


def _dep_id(project: Path) -> str:
    routes, _audit, _graph, _notes = inventory_routes(
        project, codegraph="off", openapi_source=str(project / "openapi.json")
    )
    context = build_context(
        project,
        [DOC],
        openapi=str(project / "openapi.json"),
        routes=routes,
    )
    for profile in context["profiles"]:
        if profile["endpoint"] == "POST /api/v1/quotes":
            return profile["downstream_deps"][0]["dep_id"]
    raise AssertionError("downstream dep not found")


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
            if section["section"].endswith("POST /api/v1/quotes"):
                return section["section"]
    raise AssertionError("section not found")


SUCCESS_TEXT = "报价总额等于下游返回的单价乘以数量"
NOT_FOUND_TEXT = "当价格服务返回 404 时，报价接口返回 404"


def _rule_refs(section: str) -> tuple[dict, dict, str, str]:
    success_key = rule_key(DOC, section, SUCCESS_TEXT)
    missing_key = rule_key(DOC, section, NOT_FOUND_TEXT)
    success = {
        "rule_key": success_key,
        "rule_id": "BR-01",
        "authority": "A",
        "source_kind": "explicit",
    }
    missing = {
        "rule_key": missing_key,
        "rule_id": "BR-02",
        "authority": "A",
        "source_kind": "explicit",
    }
    return success, missing, success_key, missing_key


def _rules(section: str) -> dict:
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "summary": "报价接口依赖价格服务",
        "rules": [
            {
                "text": SUCCESS_TEXT,
                "type": "calculation",
                "entities": ["报价"],
                "fields": ["total_cents", "quantity"],
                "source": {
                    "doc": DOC,
                    "section": section,
                    "quote": SUCCESS_TEXT,
                    "kind": "explicit",
                },
                "authority": "A",
                "impact": "P0-阻断",
                "priority": "P0",
            },
            {
                "text": NOT_FOUND_TEXT,
                "type": "error_handling",
                "entities": ["报价", "价格服务"],
                "fields": ["sku"],
                "source": {
                    "doc": DOC,
                    "section": section,
                    "quote": NOT_FOUND_TEXT,
                    "kind": "explicit",
                },
                "authority": "A",
                "impact": "P1-功能",
                "priority": "P1",
            },
        ],
        "questions": [],
        "gaps": [],
        "conflicts": [],
    }


def _mock_semantics(dep_id: str, section: str) -> dict:
    success_ref, _missing_ref, _success_key, _missing_key = _rule_refs(section)
    return {
        "deps": [
            {
                "dep_id": dep_id,
                "branches": [
                    {
                        "branch": "success",
                        "semantics": "价格存在",
                        "stub": {
                            "status": 200,
                            "body": {"unit_price_cents": 2500},
                        },
                        "evidence": "inferred",
                        "source": {
                            "kind": "rule",
                            "rule_keys": [success_ref["rule_key"]],
                        },
                    }
                ],
            }
        ],
        "gaps": [],
        "questions": [],
    }


def _mock_semantics_bad_body(dep_id: str, section: str) -> dict:
    payload = _mock_semantics(dep_id, section)
    payload["deps"][0]["branches"][0]["stub"]["body"] = {"price": 2500}
    return payload


def _draft(
    dep_id: str,
    section: str,
    total_cents: int = 5000,
    failure_branch: str = "rule_BR-02",
) -> dict:
    success_ref, missing_ref, _success_key, _missing_key = _rule_refs(section)
    return {
        "schema_version": 1,
        "context_id": "placeholder",
        "cases": [
            {
                "case_key": "quote_success",
                "title": "报价成功",
                "level": "business",
                "requirement_refs": [
                    {
                        "doc": DOC,
                        "section": section,
                        "quote": SUCCESS_TEXT,
                    }
                ],
                "rule_refs": [success_ref],
                "endpoint": "POST /api/v1/quotes",
                "category": "positive",
                "body": {"sku": "ABC-1", "quantity": 2},
                "asserts": [
                    {"kind": "status_code", "op": "eq", "expect": 200},
                    {
                        "kind": "body_field",
                        "target": "total_cents",
                        "op": "eq",
                        "expect": total_cents,
                    },
                ],
                "mock_bindings": {dep_id: "success"},
                "rationale": "单价 2500 × 2",
            },
            {
                "case_key": "quote_sku_missing",
                "title": "商品不存在",
                "level": "business",
                "requirement_refs": [
                    {
                        "doc": DOC,
                        "section": section,
                        "quote": NOT_FOUND_TEXT,
                    }
                ],
                "rule_refs": [missing_ref],
                "endpoint": "POST /api/v1/quotes",
                "category": "negative",
                "body": {"sku": "ABC-404", "quantity": 1},
                "asserts": [{"kind": "status_code", "op": "eq", "expect": 404}],
                "mock_bindings": {dep_id: failure_branch},
                "rationale": "下游 404 透传",
            },
        ],
    }


class _Usage:
    def as_dict(self) -> dict:
        return {"prompt_chars": 10, "attempts": 1}


class ScriptedLLM:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls: list[dict[str, str]] = []
        self.config = SimpleNamespace(redacted=lambda: {"provider": "fake", "model": "fake"})
        self.usage = [_Usage()]

    def complete_json(self, *, system: str, user: str):
        self.calls.append({"system": system, "user": user})
        return self.payloads.pop(0)


def _config(project: Path, port: int, *, approve: str | None) -> FlowConfig:
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
        approve_as=approve,
        out=".astsight/agent",
        timeout=180.0,
        sut_timeout=30.0,
        allow_unmapped=True,
    )


def test_agent_flow_runs_with_injected_mock_and_approval(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    dep_id = _dep_id(project)
    section = _section(project)
    port = _free_port()
    llm = ScriptedLLM([_rules(section), _mock_semantics(dep_id, section), _draft(dep_id, section)])

    result = run_flow(_config(project, port, approve="tester"), llm=llm)

    assert result.status == "pass", (result.steps, result.warnings, result.report)
    assert result.report is not None
    assert result.report["gate"]["verdict"] == "pass"
    records = json.loads(
        (Path(result.run_dir) / "execution.json").read_text(encoding="utf-8")
    )["records"]
    assert len(records) == 2
    assert all(item["outcome"] == "passed" for item in records)

    store = json.loads((project / "caseset.json").read_text(encoding="utf-8"))
    assert store["store_version"] == 1
    assert len(store["cases"]) == 2
    assert all(item["status"] == "approved" for item in store["cases"])

    manifest = json.loads(
        (Path(result.run_dir) / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert "token" not in manifest
    assert manifest["token_fingerprint"]

    injection = json.loads(
        (Path(result.run_dir) / "mock_injection.json").read_text(encoding="utf-8")
    )
    assert injection["injections"][0]["property"] == "PRICING_BASE_URL"
    assert injection["injections"][0]["strategy"] == "url_override"

    evidence = json.loads(
        (Path(result.run_dir) / "mock_evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["checked"] is True
    assert evidence["calls"] >= 2
    assert evidence.get("unused_binding_count", 0) == 0
    assert not (evidence.get("by_result") or {}).get("unexpected_dep")


def test_agent_flow_defaults_to_three_repair_rounds(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    config = _config(project, _free_port(), approve=None)
    assert config.max_repair_rounds == 3


def test_agent_flow_stops_at_pending_proposal(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    dep_id = _dep_id(project)
    section = _section(project)
    port = _free_port()
    llm = ScriptedLLM([_rules(section), _mock_semantics(dep_id, section), _draft(dep_id, section)])

    result = run_flow(_config(project, port, approve=None), llm=llm)

    assert result.status == "pending_review"
    assert result.steps["proposal"]["path"]
    assert not (project / "caseset.json").exists()
    assert not (Path(result.run_dir) / "execution.json").exists()


def test_agent_flow_repairs_case_error_and_reruns(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    dep_id = _dep_id(project)
    section = _section(project)
    port = _free_port()
    diagnosis_and_repair = {
        "schema_version": 2,
        "diagnoses": [
            {
                "case_id": "TC-0002",
                "phenomenon": "期望总额 9999，实际 5000",
                "root_cause": "case_error",
                "action_target": "user",
                "confidence": "high",
                "evidence": [
                    {"fact": "stub 单价 2500 × 2 = 5000", "source": "mock_plan"}
                ],
                "verify_action": "把期望总额改为 5000",
                "patch": {
                    "case_key": "quote_success",
                    "reason": "Mock 单价 2500 × 数量 2 = 5000",
                    "asserts": [
                        {"kind": "status_code", "op": "eq", "expect": 200},
                        {
                            "kind": "body_field",
                            "target": "total_cents",
                            "op": "eq",
                            "expect": 5000,
                        },
                    ],
                },
            }
        ],
    }
    llm = ScriptedLLM(
        [
            _rules(section),
            _mock_semantics(dep_id, section),
            _draft(dep_id, section, total_cents=9999),
            diagnosis_and_repair,
        ]
    )
    config = _config(project, port, approve="tester")
    config.max_repair_rounds = 1

    result = run_flow(config, llm=llm)

    assert result.status == "pass", (result.steps, result.warnings, result.report)
    assert result.steps["repair_1"]["applied"][0]["case_key"] == "quote_success"
    assert result.steps["repair_1"]["verdict"] == "pass"
    assert result.steps["repair_1"]["reviewer"] == "astsight-agent:repair"
    assert len(llm.calls) == 4

    store = json.loads((project / "caseset.json").read_text(encoding="utf-8"))
    repaired = next(
        item for item in store["cases"] if item["case_key"] == "quote_success"
    )
    assert repaired["version"] == 2
    assert store["history"][0]["case_key"] == "quote_success"


def test_agent_flow_repairs_mock_structure_and_reruns(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    dep_id = _dep_id(project)
    section = _section(project)
    port = _free_port()
    draft_payload = _draft(dep_id, section)
    draft_payload["cases"] = draft_payload["cases"][:1]
    merged = {
        "schema_version": 2,
        "diagnoses": [
            {
                "case_id": "TC-0001",
                "phenomenon": "Mock 返回 price，SUT 读取 unit_price_cents 抛出 KeyError 并返回 500",
                "root_cause": "mock_error",
                "action_target": "framework",
                "confidence": "high",
                "evidence": [
                    {
                        "fact": "KeyError: 'unit_price_cents'",
                        "source": "execution.message",
                    },
                    {
                        "fact": "success 分支返回 {'price': 2500}",
                        "source": "mock_semantics",
                    },
                ],
                "verify_action": "修正 success 分支响应结构后重跑",
                "patch": {
                    "case_key": "quote_success",
                    "reason": "SUT 读取 unit_price_cents，Mock 却返回 price",
                    "mock_branches": [
                        {
                            "dep_id": dep_id,
                            "branch": "success",
                            "reason": "与 SUT 消费字段对齐",
                            "body": {"unit_price_cents": 2500},
                        }
                    ],
                },
            }
        ],
    }
    llm = ScriptedLLM(
        [
            _rules(section),
            _mock_semantics_bad_body(dep_id, section),
            draft_payload,
            merged,
        ]
    )
    config = _config(project, port, approve="tester")
    config.max_repair_rounds = 1

    result = run_flow(config, llm=llm)

    assert result.status == "pass", (result.steps, result.warnings, result.report)
    assert result.steps["repair_1"]["mock_hot_updated"]
    assert result.steps["repair_1"]["run_dir"]
    assert len(llm.calls) == 4
    store = json.loads((project / "caseset.json").read_text(encoding="utf-8"))
    assert store["cases"][0]["version"] == 1
    semantics = json.loads(
        (project / ".astsight" / "mock_semantics.json").read_text(encoding="utf-8")
    )
    branch = next(
        item
        for dep in semantics["deps"]
        for item in dep["branches"]
        if item["branch"] == "success"
    )
    assert branch["stub"]["body"] == {"unit_price_cents": 2500}
