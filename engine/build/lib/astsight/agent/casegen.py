"""A4 用例草案生成：semantic analysis → CaseDraft。

草案仍走既有的 `validate_draft`（端点/字段/quote/断言组合/资源上限），
并额外核对 mock_bindings 的 dep_id 与分支是否来自 context + semantic analysis。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..cases.draft import CaseDraft
from ..cases.validate import ValidationResult, validate_draft
from ..llm import LLMClient, LLMError
from .errors import AgentError
from .models import SemanticAnalysis

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
# 旧链路提示词：只保留模块兼容，不再被规则驱动主流程调用。
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "legacy" / "agent_case_generation.md"
MAX_CASES = 200


def load_casegen_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else _PROMPT_PATH
    if not prompt_path.is_file():
        raise AgentError("casegen_prompt_missing", str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _profile_index(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(profile.get("endpoint") or ""): profile
        for profile in context.get("profiles") or []
    }


def _dep_index(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(dep.get("dep_id") or ""): dep
        for dep in profile.get("downstream_deps") or []
        if dep.get("dep_id")
    }


def _auto_bind_single_dependency(
    draft: CaseDraft,
    analysis: SemanticAnalysis,
    context: dict[str, Any],
) -> list[str]:
    """仅对“恰好一个下游 + 正向/业务 + LLM 未绑定”补 success，且显式告警。"""
    warnings: list[str] = []
    profiles = _profile_index(context)
    branches = analysis.branch_index()
    for case in draft.cases:
        if case.mock_bindings:
            continue
        if case.category not in ("positive", "business"):
            continue
        profile = profiles.get(case.endpoint)
        if profile is None:
            continue
        deps = _dep_index(profile)
        if len(deps) != 1:
            continue
        dep_id = next(iter(deps))
        if (dep_id, "success") not in branches:
            continue
        case.mock_bindings = {dep_id: "success"}
        warnings.append(
            f"default_success_binding:{case.case_key}:{dep_id}"
        )
    return warnings


def validate_case_bindings(
    draft: CaseDraft,
    analysis: SemanticAnalysis,
    context: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)；只核对下游绑定与证据，不改写用例。"""
    errors: list[str] = []
    warnings: list[str] = []
    branches = analysis.branch_index()
    profiles = _profile_index(context)
    for case in draft.cases:
        profile = profiles.get(case.endpoint)
        if profile is None:
            continue
        deps = _dep_index(profile)
        for dep_id, branch in case.mock_bindings.items():
            if dep_id not in deps:
                errors.append(
                    f"{case.case_key}: mock dep_id 不属于 {case.endpoint}: {dep_id!r}"
                )
            if (dep_id, branch) not in branches:
                errors.append(
                    f"{case.case_key}: 未定义的分支 {dep_id} / {branch}"
                )
        if deps and case.category in ("positive", "business") and not case.mock_bindings:
            warnings.append(
                f"downstream_unbound:{case.case_key}:{case.endpoint}"
            )
    return errors, warnings


def _prompt_input(context: dict[str, Any], analysis: SemanticAnalysis) -> dict[str, Any]:
    return {
        "context": {
            "context_id": context.get("context_id"),
            "documents": context.get("documents") or [],
            "profiles": context.get("profiles") or [],
            "profile_unmapped": context.get("profile_unmapped") or [],
            "openapi": context.get("openapi") or {},
        },
        "semantic_analysis": analysis.model_dump(mode="json"),
        "schema": CaseDraft.model_json_schema(),
    }


def generate_case_draft(
    context: dict[str, Any],
    analysis: SemanticAnalysis,
    *,
    llm: LLMClient,
    project: str | Path,
    prompt_path: str | Path | None = None,
    max_cases: int = MAX_CASES,
) -> tuple[CaseDraft, ValidationResult, list[str]]:
    system = load_casegen_prompt(prompt_path)
    user = json.dumps(
        _prompt_input(context, analysis),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("casegen_llm_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise AgentError("casegen_invalid", "模型输出必须是 JSON object")
    try:
        draft = CaseDraft.model_validate(payload)
    except Exception as exc:
        raise AgentError("casegen_invalid", str(exc)[:2_000]) from exc
    if len(draft.cases) > max_cases:
        raise AgentError(
            "casegen_too_many_cases",
            f"{len(draft.cases)} > {max_cases}",
        )

    draft.context_id = str(context.get("context_id") or "")
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

    warnings = _auto_bind_single_dependency(draft, analysis, context)
    validation = validate_draft(draft, context, project=project)
    errors = [item.message for item in validation.errors]
    binding_errors, binding_warnings = validate_case_bindings(
        draft, analysis, context
    )
    errors.extend(binding_errors)
    warnings.extend(binding_warnings)
    warnings.extend(item.message for item in validation.warnings)
    if errors:
        raise AgentError(
            "casegen_validation_failed",
            "；".join(errors[:20]),
            details={
                "errors": errors,
                "warnings": warnings,
                "draft": draft.model_dump(mode="json"),
            },
        )
    return draft, validation, warnings


__all__ = [
    "MAX_CASES",
    "generate_case_draft",
    "load_casegen_prompt",
    "validate_case_bindings",
]
