"""A1 规则抽取适配器：规则 context → LLM → RuleDraft → 确定性导入。

模型只提出候选；rule_key、fingerprint、A 级落地、CR 契约规则都由
`astsight.rules` 重算。校验不通过时不产生可写规则库。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..llm import LLMClient, LLMError
from ..rules import (
    RuleDraft,
    RuleImportResult,
    import_rule_draft,
    load_rule_set,
)
from ..rules.store import RuleStoreError
from .errors import AgentError

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "a1_requirement_analysis.md"


def load_rule_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else _PROMPT_PATH
    if not prompt_path.is_file():
        raise AgentError("rules_prompt_missing", str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _context_view(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "context_id": context.get("context_id"),
        "documents": context.get("documents") or [],
        "openapi": context.get("openapi") or {},
        "operations": context.get("operations") or [],
    }


def generate_rule_draft(
    context: dict[str, Any],
    *,
    llm: LLMClient,
    prompt_path: str | Path | None = None,
    max_rules: int = 500,
) -> RuleDraft:
    system = load_rule_prompt(prompt_path)
    user = json.dumps(
        {
            "context": _context_view(context),
            "schema": RuleDraft.model_json_schema(),
            "notes": [
                "source.section 必须取自 context.documents[].sections[].path 或 number",
                "source.quote 必须是该 section 原文的连续子串",
                "gaps 每项必须包含 reason",
                "不要输出 ContractRule；CR 规则由框架从 OpenAPI 确定性生成",
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("rules_llm_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise AgentError("rules_invalid", "模型输出必须是 JSON object")
    try:
        draft = RuleDraft.model_validate(payload)
    except Exception as exc:
        raise AgentError("rules_invalid", str(exc)[:2_000]) from exc
    if len(draft.rules) > max_rules:
        raise AgentError("rules_too_many", f"{len(draft.rules)} > {max_rules}")
    # context_id 由框架回填，其余来源证据仍逐项校验。
    draft.context_id = str(context.get("context_id") or "")
    return draft


def import_rule_draft_for_flow(
    *,
    project: str,
    context: dict[str, Any],
    draft: RuleDraft,
    previous_path: str | Path | None = None,
    prompt: dict[str, Any] | None = None,
) -> RuleImportResult:
    previous = None
    if previous_path is not None:
        try:
            previous = load_rule_set(previous_path)
        except RuleStoreError as exc:
            raise AgentError("rules_base_invalid", str(exc)) from exc
    result = import_rule_draft(
        project=project,
        context=context,
        draft=draft,
        previous=previous,
        prompt=prompt
        or {
            "name": "a1_requirement_analysis",
            "version": "2",
        },
        dry_run=True,
    )
    if result.validation.errors:
        raise AgentError(
            "rules_validation_failed",
            "；".join(result.validation.errors[:20]),
            details={
                "errors": result.validation.errors,
                "warnings": result.validation.warnings,
                "draft": draft.model_dump(mode="json"),
            },
        )
    return result


__all__ = [
    "generate_rule_draft",
    "import_rule_draft_for_flow",
    "load_rule_prompt",
]
