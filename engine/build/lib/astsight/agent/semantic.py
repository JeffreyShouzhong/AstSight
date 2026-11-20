"""A1 语义分析：context + 变更范围 → 行为清单与下游分支语义。

LLM 只负责提出候选；endpoint / section / quote / dep_id 由本模块逐项核对。
任一硬性证据不成立即拒绝，不把模型输出直接送入用例生成。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cases.requirements import normalize_text
from ..llm import LLMClient, LLMError
from .errors import AgentError
from .models import (
    BRANCH_RE,
    SemanticAnalysis,
    SemanticBehavior,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
# 旧链路提示词：只保留模块兼容，不再被规则驱动主流程调用。
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "legacy" / "a1_semantic_analysis.md"


@dataclass
class SemanticValidation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_semantic_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else _PROMPT_PATH
    if not prompt_path.is_file():
        raise AgentError("semantic_prompt_missing", str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _profile_index(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(profile.get("endpoint") or ""): profile
        for profile in context.get("profiles") or []
    }


def _section_index(context: dict[str, Any]) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for document in context.get("documents") or []:
        doc = str(document.get("doc") or "")
        for section in document.get("sections") or []:
            index[(doc, str(section.get("section") or ""))] = str(
                section.get("text") or ""
            )
    return index


def _dep_ids(profile: dict[str, Any]) -> set[str]:
    return {
        str(dep.get("dep_id") or "")
        for dep in profile.get("downstream_deps") or []
        if dep.get("dep_id")
    }


def _grounded(codes: list[int], quote: str, profile: dict[str, Any]) -> bool:
    allowed = {int(code) for code in profile.get("error_codes") or []}
    if any(code in allowed for code in codes):
        return True
    lowered = normalize_text(quote).lower()
    return any(str(code) in lowered for code in codes)


def validate_semantics(
    analysis: SemanticAnalysis, context: dict[str, Any]
) -> SemanticValidation:
    result = SemanticValidation()
    context_id = str(context.get("context_id") or "")
    if analysis.context_id != context_id:
        result.errors.append(
            f"context_id 不一致: {analysis.context_id!r} != {context_id!r}"
        )
    profiles = _profile_index(context)
    sections = _section_index(context)
    branch_keys = {(item.dep_id, item.branch) for item in analysis.downstream_branches}
    if len(branch_keys) != len(analysis.downstream_branches):
        result.errors.append("downstream_branches 存在重复的 (dep_id, branch)")
    all_dep_ids: set[str] = set()
    for profile in profiles.values():
        all_dep_ids.update(_dep_ids(profile))

    seen_behaviors: set[str] = set()
    for behavior in analysis.behaviors:
        where = f"behaviors[{behavior.behavior_key}]"
        if behavior.behavior_key in seen_behaviors:
            result.errors.append(f"{where}: behavior_key 重复")
        seen_behaviors.add(behavior.behavior_key)
        profile = profiles.get(behavior.endpoint)
        if profile is None:
            result.errors.append(f"{where}: endpoint 不在 context: {behavior.endpoint!r}")
            continue
        quotes: list[str] = []
        for ref in behavior.requirement_refs:
            body = sections.get((ref.doc, ref.section))
            if body is None:
                result.errors.append(
                    f"{where}: 需求 section 不存在: {ref.doc}#{ref.section}"
                )
                continue
            quote = normalize_text(ref.quote)
            if len(quote) < 4:
                result.errors.append(f"{where}: quote 过短: {ref.quote!r}")
            elif quote not in normalize_text(body):
                result.errors.append(
                    f"{where}: quote 不在 section 原文中: {ref.quote!r}"
                )
            else:
                quotes.append(quote)
        for item in behavior.downstream:
            if item.dep_id not in _dep_ids(profile):
                result.errors.append(
                    f"{where}: dep_id 不属于 {behavior.endpoint}: {item.dep_id!r}"
                )
            if (item.dep_id, item.branch) not in branch_keys:
                result.errors.append(
                    f"{where}: 缺少分支定义 {item.dep_id} / {item.branch}"
                )
        non_2xx = [code for code in behavior.expected_status if not 200 <= code < 300]
        if non_2xx:
            joined = " ".join(quotes)
            if not _grounded(non_2xx, joined, profile) and not any(
                str(code) in joined for code in non_2xx
            ):
                result.warnings.append(
                    f"{where}: 非 2xx 期望 {non_2xx} 未在 quote/OpenAPI error_codes 中找到依据"
                )

    for item in analysis.downstream_branches:
        where = f"downstream_branches[{item.dep_id}/{item.branch}]"
        if item.dep_id not in all_dep_ids:
            result.errors.append(f"{where}: dep_id 不在任何 profile 的 downstream_deps 中")
        if not BRANCH_RE.match(item.branch):
            result.errors.append(f"{where}: branch 命名不合法")

    for item in analysis.uncovered_requirements:
        doc = str(item.get("doc") or "")
        section = str(item.get("section") or "")
        reason = str(item.get("reason") or "").strip()
        if (doc, section) not in sections:
            result.errors.append(
                f"uncovered_requirements: section 不存在: {doc}#{section}"
            )
        if not reason:
            result.errors.append(f"uncovered_requirements: {doc}#{section} 缺少 reason")
    return result


def _prompt_input(
    context: dict[str, Any], changed_apis: list[dict[str, Any]] | None
) -> dict[str, Any]:
    return {
        "context": {
            "context_id": context.get("context_id"),
            "documents": context.get("documents") or [],
            "profiles": context.get("profiles") or [],
            "profile_unmapped": context.get("profile_unmapped") or [],
            "openapi": context.get("openapi") or {},
        },
        "changed_apis": changed_apis or [],
        "schema": SemanticAnalysis.model_json_schema(),
    }


def analyze_semantics(
    context: dict[str, Any],
    *,
    llm: LLMClient,
    changed_apis: list[dict[str, Any]] | None = None,
    prompt_path: str | Path | None = None,
) -> tuple[SemanticAnalysis, SemanticValidation]:
    system = load_semantic_prompt(prompt_path)
    user = json.dumps(
        _prompt_input(context, changed_apis),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("semantic_llm_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise AgentError("semantic_invalid", "模型输出必须是 JSON object")
    try:
        analysis = SemanticAnalysis.model_validate(payload)
    except Exception as exc:
        raise AgentError("semantic_invalid", str(exc)[:2_000]) from exc
    # context_id 由框架回填（与 casegen 一致），避免模型抄错导致整轮失败；
    # 其余 endpoint/quote/dep/branch 证据仍然逐项校验。
    analysis.context_id = str(context.get("context_id") or "")
    validation = validate_semantics(analysis, context)
    if not validation.ok:
        raise AgentError(
            "semantic_evidence_invalid",
            "；".join(validation.errors[:20]),
            details=validation.errors,
        )
    return analysis, validation


def describe_behaviors(analysis: SemanticAnalysis) -> dict[str, Any]:
    """给用例生成/报告的稳定摘要。"""
    return {
        "summary": analysis.summary,
        "behaviors": [
            {
                "behavior_key": item.behavior_key,
                "endpoint": item.endpoint,
                "title": item.title,
                "category": item.category,
                "expected_status": item.expected_status,
                "downstream": [dep.model_dump() for dep in item.downstream],
                "risk": item.risk,
            }
            for item in analysis.behaviors
        ],
        "downstream_branches": [
            {
                "dep_id": item.dep_id,
                "branch": item.branch,
                "semantics": item.semantics,
            }
            for item in analysis.downstream_branches
        ],
        "uncovered_requirements": list(analysis.uncovered_requirements),
        "questions": list(analysis.questions),
    }


__all__ = [
    "SemanticValidation",
    "analyze_semantics",
    "describe_behaviors",
    "load_semantic_prompt",
    "validate_semantics",
]
