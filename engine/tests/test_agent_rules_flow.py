"""规则驱动 Agent 编排：rules/mock semantics 落盘，待审阶段不进入 C5b/C6。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.agent.flow import run_flow
from astsight.cases.context import build_context
from astsight.inventory import inventory_routes
from astsight.rules.store import load_rule_set, rule_set_snapshot
from test_agent_flow import (
    DOC,
    ScriptedLLM,
    _config,
    _copy_example,
    _dep_id,
    _draft,
    _mock_semantics,
    _rules,
    _section,
)


def _code_context(project: Path) -> dict:
    routes, _audit, _graph, _notes = inventory_routes(project, codegraph="off")
    return build_context(project, [DOC], routes=routes)


def _section_without_openapi(project: Path) -> str:
    for document in _code_context(project)["documents"]:
        for section in document["sections"]:
            if section["section"].endswith("POST /api/v1/quotes"):
                return section["section"]
    raise AssertionError("section not found")


def _dep_id_without_openapi(project: Path) -> str:
    for profile in _code_context(project)["profiles"]:
        if profile["endpoint"] == "POST /api/v1/quotes":
            return profile["downstream_deps"][0]["dep_id"]
    raise AssertionError("downstream dep not found")


def test_rules_and_design_run_without_openapi(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    (project / "openapi.json").unlink()
    dep_id = _dep_id_without_openapi(project)
    section = _section_without_openapi(project)
    config = _config(project, 0, approve=None)
    config.openapi = None
    llm = ScriptedLLM(
        [_rules(section), _mock_semantics(dep_id, section), _draft(dep_id, section)]
    )

    result = run_flow(config, llm=llm)

    assert result.status == "pending_review", (result.steps, result.errors)
    assert result.steps["scope"]["interface_evidence"] == "code_routes_only"
    assert result.steps["scope"]["contract_incomplete"] >= 1
    assert result.steps["rules"]["contract_rules"] == 0
    assert any(warning.startswith("openapi_missing") for warning in result.warnings)
    assert (project / "ruleset.json").is_file()
    assert Path(result.steps["proposal"]["path"]).is_dir()


def test_rules_and_mock_semantics_are_written_before_design(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    dep_id = _dep_id(project)
    section = _section(project)
    llm = ScriptedLLM(
        [_rules(section), _mock_semantics(dep_id, section), _draft(dep_id, section)]
    )

    result = run_flow(_config(project, 0, approve=None), llm=llm)

    assert result.status == "pending_review", (result.steps, result.errors)
    rules_step = result.steps["rules"]
    assert rules_step["business_rules"] == 2
    assert rules_step["contract_rules"] > 0
    ruleset = load_rule_set(project / "ruleset.json")
    assert len(ruleset.rules) == 2
    assert ruleset.contract_rules
    assert {rule.authority.value for rule in ruleset.rules} <= {"A", "B"}
    review = Path(rules_step["review"])
    assert review == project / "ruleset.md"
    assert "## 2 规则表" in review.read_text(encoding="utf-8")

    mock_step = result.steps["mock_semantics"]
    assert mock_step["deps"] == 1
    assert mock_step["branches"] >= 2  # 契约 success + BR-02 规则分支
    semantics = json.loads(
        Path(mock_step["path"]).read_text(encoding="utf-8")
    )
    dep = next(item for item in semantics["deps"] if item["dep_id"] == dep_id)
    assert dep["branches"][0]["branch"] == "success"

    proposal_dir = Path(result.steps["proposal"]["path"])
    snapshot = json.loads(
        (proposal_dir / "rules_snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot["ruleset_id"] == ruleset.ruleset_id
    assert snapshot["version"] == ruleset.version
    proposal = json.loads(
        (proposal_dir / "proposal.json").read_text(encoding="utf-8")
    )
    assert proposal["base_ruleset"]["hash"] == rule_set_snapshot(ruleset)["hash"]

    # 人工审核前不得产生已批准用例、造数计划或执行结果。
    assert not (project / "caseset.json").exists()
    assert not (project / ".astsight" / "data" / "data_plan.json").exists()
    assert not (Path(result.run_dir) / "execution.json").exists()
    assert not (Path(result.run_dir) / "data_plan.json").exists()


def test_pending_review_blocks_c5b_even_with_catalog(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    (project / "schema.sql").write_text(
        "CREATE TABLE quotes (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  sku TEXT NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    dep_id = _dep_id(project)
    section = _section(project)
    config = _config(project, 0, approve=None)
    config.data_ddl = ["schema.sql"]
    llm = ScriptedLLM(
        [
            _rules(section),
            _mock_semantics(dep_id, section),
            {
                "schema_version": 1,
                "field_semantics": [],
                "states": [],
                "gaps": [],
                "questions": [],
            },
            _draft(dep_id, section),
        ]
    )

    result = run_flow(config, llm=llm)

    assert result.status == "pending_review", (result.steps, result.errors)
    assert result.steps["data_catalog"]["tables"] == 1
    assert (project / ".astsight" / "data" / "data_catalog.json").is_file()
    assert not (project / ".astsight" / "data" / "data_plan.json").exists()
    assert not (project / "caseset.json").exists()
