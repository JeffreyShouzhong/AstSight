"""A4 用例设计适配器：规则 + 画像 + Mock 语义 + 数据目录 → CaseDraft。

模型只做场景联想；接口、字段、规则引用、Mock 分支和前置状态全部逐项
对事实源校验。规则库是唯一业务规则来源，草案不得新增规则。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import DataCatalog, MockSemantic, RuleSet, RuleStatus

from ..cases.draft import CaseDraft
from ..cases.validate import ValidationResult, validate_draft
from ..llm import LLMClient, LLMError
from ..rules import rule_set_hash
from .errors import AgentError

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "a4_case_design.md"
MAX_CASES = 200
MAX_RULES_IN_PROMPT = 300


@dataclass
class CaseDesignResult:
    draft: CaseDraft
    validation: ValidationResult
    warnings: list[str] = field(default_factory=list)


def load_case_design_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else _PROMPT_PATH
    if not prompt_path.is_file():
        raise AgentError("case_design_prompt_missing", str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _rule_view(ruleset: RuleSet) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for rule in ruleset.rules:
        if rule.status is RuleStatus.DEPRECATED:
            continue
        items.append(
            {
                "rule_key": rule.rule_key,
                "rule_id": rule.rule_id,
                "text": rule.text,
                "type": rule.type.value,
                "entities": list(rule.entities),
                "fields": list(rule.fields),
                "authority": rule.authority.value,
                "source_kind": rule.source.kind.value,
                "priority": rule.priority.value,
                "status": rule.status.value,
            }
        )
    for rule in ruleset.contract_rules:
        if rule.status is RuleStatus.DEPRECATED:
            continue
        items.append(
            {
                "rule_key": rule.rule_key,
                "rule_id": rule.rule_id,
                "text": f"{rule.endpoint} {rule.field}: {rule.constraint}",
                "type": "contract",
                "transport": rule.transport.value,
                "endpoint": rule.endpoint,
                "field": rule.field,
                "constraint": rule.constraint,
                "authority": rule.authority.value,
                "source_kind": "explicit",
                "priority": "P1",
                "status": rule.status.value,
            }
        )
    return items[:MAX_RULES_IN_PROMPT]


def _semantic_view(semantic: MockSemantic | None) -> list[dict[str, Any]]:
    if semantic is None:
        return []
    return [
        {
            "dep_id": dep.dep_id,
            "endpoint": dep.endpoint,
            "route_key": dep.route_key,
            "strategy": dep.strategy.value,
            "branches": [
                {
                    "branch": branch.branch,
                    "semantics": branch.semantics,
                    "source_kind": branch.source.kind,
                    "rule_keys": list(branch.source.rule_keys),
                }
                for branch in dep.branches
            ],
            "gap_reason": [
                gap.reason for gap in semantic.gaps if gap.dep_id == dep.dep_id
            ],
        }
        for dep in semantic.deps
    ]


def _catalog_view(catalog: DataCatalog | None) -> dict[str, Any] | None:
    if catalog is None:
        return None
    return {
        "catalog_id": catalog.catalog_id,
        "tables": [
            {
                "name": table.name,
                "primary_key": list(table.primary_key),
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "nullable": column.nullable,
                        "enum_values": list(column.enum_values),
                        "semantic_type": column.semantic_type.value,
                    }
                    for column in table.columns
                ],
            }
            for table in catalog.tables
        ],
        "states": [
            {
                "state_key": state.state_key,
                "target_state": state.target_state,
                "table_values": state.table_values,
                "rule_keys": list(state.rule_keys),
            }
            for state in catalog.states
        ],
        "gaps": catalog.gaps,
    }


def _profile_view(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "endpoint": profile.get("endpoint"),
        "method": profile.get("method"),
        "path": profile.get("path"),
        "transport": profile.get("transport", "http"),
        "coverage_role": profile.get("coverage_role", "primary"),
        "business_key": profile.get("business_key"),
        "request_fields": profile.get("request_fields") or [],
        "response_fields": profile.get("response_fields") or [],
        "success_codes": profile.get("success_codes") or [],
        "error_codes": profile.get("error_codes") or [],
        "downstream_deps": profile.get("downstream_deps") or [],
    }


def _prompt_input(
    context: dict[str, Any],
    *,
    ruleset: RuleSet,
    mode: str,
    changed_apis: list[dict[str, Any]] | None,
    mock_semantic: MockSemantic | None,
    data_catalog: DataCatalog | None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "context_id": context.get("context_id"),
        "profiles": [_profile_view(item) for item in context.get("profiles") or []],
        "profile_unmapped": context.get("profile_unmapped") or [],
        "documents": context.get("documents") or [],
        "openapi": context.get("openapi") or {},
        "changed_apis": changed_apis or [],
        "ruleset": {
            "ruleset_id": ruleset.ruleset_id,
            "version": ruleset.version,
            "hash": rule_set_hash(ruleset),
            "rules": _rule_view(ruleset),
            "unresolved_conflicts": sum(
                1 for conflict in ruleset.conflicts if not conflict.resolved_at
            ),
        },
        "mock_semantics": _semantic_view(mock_semantic),
        "data_catalog": _catalog_view(data_catalog),
        "schema": CaseDraft.model_json_schema(),
    }


def _auto_bind_single_dependency(
    draft: CaseDraft, semantic: MockSemantic | None, context: dict[str, Any]
) -> list[str]:
    warnings: list[str] = []
    if semantic is None:
        return warnings
    branches = semantic.branch_index()
    profiles = {
        str(profile.get("endpoint") or ""): profile
        for profile in context.get("profiles") or []
    }
    for case in draft.cases:
        if case.mock_bindings:
            continue
        if case.category not in ("positive", "business", "smoke"):
            continue
        profile = profiles.get(case.endpoint)
        if profile is None:
            continue
        deps = [
            str(dep.get("dep_id") or "")
            for dep in profile.get("downstream_deps") or []
            if dep.get("dep_id")
        ]
        if len(deps) != 1:
            continue
        dep_id = deps[0]
        if (dep_id, "success") not in branches:
            continue
        case.mock_bindings = {dep_id: "success"}
        warnings.append(f"default_success_binding:{case.case_key}:{dep_id}")
    return warnings


def generate_case_design(
    context: dict[str, Any],
    *,
    ruleset: RuleSet,
    llm: LLMClient,
    project: str | Path,
    mode: str = "full",
    changed_apis: list[dict[str, Any]] | None = None,
    mock_semantic: MockSemantic | None = None,
    data_catalog: DataCatalog | None = None,
    prompt_path: str | Path | None = None,
    max_cases: int = MAX_CASES,
) -> CaseDesignResult:
    if ruleset is None:
        raise AgentError("rules_missing", "A4 需要规则库，禁止无规则生成用例")
    system = load_case_design_prompt(prompt_path)
    user = json.dumps(
        _prompt_input(
            context,
            ruleset=ruleset,
            mode=mode,
            changed_apis=changed_apis,
            mock_semantic=mock_semantic,
            data_catalog=data_catalog,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("case_design_llm_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise AgentError("case_design_invalid", "模型输出必须是 JSON object")
    try:
        draft = CaseDraft.model_validate(payload)
    except Exception as exc:
        raise AgentError("case_design_invalid", str(exc)[:2_000]) from exc
    if len(draft.cases) > max_cases:
        raise AgentError(
            "case_design_too_many_cases", f"{len(draft.cases)} > {max_cases}"
        )

    draft.context_id = str(context.get("context_id") or "")
    draft.ruleset_id = ruleset.ruleset_id
    draft.ruleset_hash = rule_set_hash(ruleset)
    draft.generator = {
        **(draft.generator or {}),
        "kind": "llm",
        "name": str((draft.generator or {}).get("name") or "astsight-a4"),
    }
    if not draft.scope_docs:
        draft.scope_docs = sorted(
            {
                ref.doc
                for case in draft.cases
                for ref in case.requirement_refs
                if ref.doc
            }
        )

    warnings = _auto_bind_single_dependency(draft, mock_semantic, context)
    validation = validate_draft(
        draft,
        context,
        project=project,
        ruleset=ruleset,
        data_catalog=data_catalog,
        mock_semantic=mock_semantic,
        require_rules=True,
    )
    errors = [item.message for item in validation.errors]
    warnings.extend(item.message for item in validation.warnings)
    if errors:
        raise AgentError(
            "case_design_validation_failed",
            "；".join(errors[:20]),
            details={
                "errors": errors,
                "warnings": warnings,
                "draft": draft.model_dump(mode="json"),
            },
        )
    return CaseDesignResult(draft=draft, validation=validation, warnings=warnings)


__all__ = [
    "MAX_CASES",
    "CaseDesignResult",
    "generate_case_design",
    "load_case_design_prompt",
]
