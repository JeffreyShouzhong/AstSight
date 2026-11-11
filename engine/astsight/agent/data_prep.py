"""C5 LLM 适配器：字段业务语义（C5a）与逐用例造数候选（C5b）。

模型只产出语义/取值候选，不产出 SQL。表/列/枚举、类型、FK 闭包、
preconditions 覆盖和 SQL 渲染全部由 `astsight.data` 确定性校验。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from contracts import DataCatalog, MockSemantic, RuleSet, TestCase
from pydantic import BaseModel, ConfigDict, Field

from ..data.semantics import SemanticApplyResult, SemanticDraft, apply_semantics
from ..llm import LLMClient, LLMError
from .errors import AgentError

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
_CATALOG_PROMPT = _AST_SIGHT_ROOT / "prompts" / "c5_data_catalog.md"
_PLAN_PROMPT = _AST_SIGHT_ROOT / "prompts" / "c5_data_plan.md"
MAX_CASES_IN_PROMPT = 200


class DataPlanDraft(BaseModel):
    """C5b 模型输出；SQL 不在契约内。"""

    model_config = ConfigDict(extra="forbid")

    plans: dict[str, dict[str, Any]] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list, max_length=200)
    questions: list[str] = Field(default_factory=list, max_length=100)


def _load_prompt(path: str | Path | None, default: Path, code: str) -> str:
    prompt_path = Path(path) if path else default
    if not prompt_path.is_file():
        raise AgentError(code, str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _catalog_payload(catalog: DataCatalog) -> dict[str, Any]:
    return {
        "catalog_id": catalog.catalog_id,
        "context_id": catalog.context_id,
        "dialect": catalog.dialect,
        "tables": [
            {
                "name": table.name,
                "comment": table.comment,
                "primary_key": list(table.primary_key),
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "nullable": column.nullable,
                        "default": column.default,
                        "enum_values": list(column.enum_values),
                        "comment": column.comment,
                        "semantic_type": column.semantic_type.value,
                    }
                    for column in table.columns
                ],
            }
            for table in catalog.tables
        ],
        "foreign_keys": [
            {
                "child_table": fk.child_table,
                "child_columns": list(fk.child_columns),
                "parent_table": fk.parent_table,
                "parent_columns": list(fk.parent_columns),
            }
            for fk in catalog.foreign_keys
        ],
        "seed_states": [
            {
                "state_key": state.state_key,
                "table_values": state.table_values,
                "target_state": state.target_state,
            }
            for state in catalog.states
        ],
        "gaps": catalog.gaps,
    }


def _rule_payload(ruleset: RuleSet | None) -> list[dict[str, Any]]:
    if ruleset is None:
        return []
    return [
        {
            "rule_key": rule.rule_key,
            "rule_id": rule.rule_id,
            "text": rule.text,
            "fields": list(rule.fields),
            "authority": rule.authority.value,
        }
        for rule in ruleset.rules
    ]


def _docs_payload(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not context:
        return []
    return [
        {
            "doc": document.get("doc"),
            "sections": [
                {
                    "section": section.get("section"),
                    "number": section.get("number"),
                    "text": str(section.get("text") or "")[:8_000],
                }
                for section in document.get("sections") or []
            ],
        }
        for document in context.get("documents") or []
    ]


def generate_data_semantics(
    catalog: DataCatalog,
    *,
    llm: LLMClient,
    ruleset: RuleSet | None = None,
    context: dict[str, Any] | None = None,
    prompt_path: str | Path | None = None,
) -> tuple[SemanticDraft, SemanticApplyResult]:
    system = _load_prompt(prompt_path, _CATALOG_PROMPT, "data_catalog_prompt_missing")
    user = json.dumps(
        {
            "catalog": _catalog_payload(catalog),
            "rules": _rule_payload(ruleset),
            "documents": _docs_payload(context),
            "schema": SemanticDraft.model_json_schema(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("data_catalog_llm_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise AgentError("data_catalog_invalid", "模型输出必须是 JSON object")
    try:
        draft = SemanticDraft.model_validate(payload)
    except Exception as exc:
        raise AgentError("data_catalog_invalid", str(exc)[:2_000]) from exc
    probe = catalog.model_copy(deep=True)
    applied = apply_semantics(probe, draft, ruleset=ruleset)
    if not applied.ok:
        raise AgentError(
            "data_catalog_candidates_invalid",
            "；".join(applied.errors[:20]),
            details={"errors": applied.errors, "warnings": applied.warnings},
        )
    return draft, applied


def _case_payload(case: TestCase) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "case_key": getattr(case, "case_key", ""),
        "title": case.title,
        "endpoint": case.endpoint,
        "rule_keys": [ref.rule_key for ref in case.rule_refs],
        "preconditions": [item.model_dump(mode="json") for item in case.preconditions],
        "request": {
            "path_params": case.path_params,
            "query": case.query,
            "headers": case.headers,
            "body": case.body,
        },
        "steps": [
            {
                "name": step.name,
                "endpoint": step.endpoint,
                "request": {
                    "path_params": step.path_params,
                    "query": step.query,
                    "headers": step.headers,
                    "body": step.body,
                },
            }
            for step in case.steps
        ],
        "mock_bindings": dict(case.mock_bindings),
    }


def generate_data_plans(
    cases: Iterable[TestCase],
    *,
    catalog: DataCatalog,
    ruleset: RuleSet | None,
    llm: LLMClient,
    mock_semantic: MockSemantic | None = None,
    prompt_path: str | Path | None = None,
) -> DataPlanDraft:
    case_list = list(cases)[:MAX_CASES_IN_PROMPT]
    system = _load_prompt(prompt_path, _PLAN_PROMPT, "data_plan_prompt_missing")
    user = json.dumps(
        {
            "catalog": _catalog_payload(catalog),
            "rules": _rule_payload(ruleset),
            "cases": [_case_payload(case) for case in case_list],
            "mock_semantics": [
                {
                    "dep_id": dep.dep_id,
                    "branches": [branch.branch for branch in dep.branches],
                }
                for dep in (mock_semantic.deps if mock_semantic else [])
            ],
            "schema": DataPlanDraft.model_json_schema(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("data_plan_llm_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise AgentError("data_plan_invalid", "模型输出必须是 JSON object")
    try:
        return DataPlanDraft.model_validate(payload)
    except Exception as exc:
        raise AgentError("data_plan_invalid", str(exc)[:2_000]) from exc


__all__ = [
    "DataPlanDraft",
    "generate_data_plans",
    "generate_data_semantics",
]
