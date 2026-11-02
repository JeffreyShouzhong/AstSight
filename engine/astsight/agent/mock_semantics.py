"""C3.5.1 Mock 语义适配器：为契约层无法补全的响应分支提供 LLM 候选。

默认 `success` 与规则映射分支已由 `astsight.mock.semantic` 确定性生成；
这里只补响应骨架/语义，候选仍逐项校验，未知 dep/分支/规则一律拒绝。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts import MockSemantic, RuleSet

from ..llm import LLMClient, LLMError
from ..mock.semantic import (
    MockSemanticApplyResult,
    apply_mock_semantic_candidates,
)
from .errors import AgentError

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "c35_mock_semantics.md"


class MockSemanticDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deps: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    gaps: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    questions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


def load_mock_semantic_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else _PROMPT_PATH
    if not prompt_path.is_file():
        raise AgentError("mock_semantics_prompt_missing", str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _needs_candidates(semantic: MockSemantic) -> bool:
    for dep in semantic.deps:
        success = next(
            (branch for branch in dep.branches if branch.branch == "success"), None
        )
        if success is None or not success.stub.body:
            return True
    return False


def generate_mock_semantic_candidates(
    context: dict[str, Any],
    semantic: MockSemantic,
    *,
    ruleset: RuleSet | None,
    llm: LLMClient,
    prompt_path: str | Path | None = None,
) -> MockSemanticApplyResult | None:
    """只为缺失/空 success body 的下游调用模型；无需要时返回 None。"""
    if not semantic.deps or not _needs_candidates(semantic):
        return None
    system = load_mock_semantic_prompt(prompt_path)
    user = json.dumps(
        {
            "context_id": context.get("context_id"),
            "profiles": context.get("profiles") or [],
            "deps": [
                {
                    "dep_id": dep.dep_id,
                    "endpoint": dep.endpoint,
                    "route_key": dep.route_key,
                    "branches": [
                        {
                            "branch": branch.branch,
                            "semantics": branch.semantics,
                            "stub": branch.stub.model_dump(mode="json"),
                            "source": branch.source.model_dump(mode="json"),
                        }
                        for branch in dep.branches
                    ],
                }
                for dep in semantic.deps
            ],
            "rules": (
                [
                    {
                        "rule_key": rule.rule_key,
                        "rule_id": rule.rule_id,
                        "text": rule.text,
                        "authority": rule.authority.value,
                    }
                    for rule in ruleset.rules
                ]
                if ruleset is not None
                else []
            ),
            "schema": MockSemanticDraft.model_json_schema(),
            "notes": [
                "只补充 branches，不新增 dep_id",
                "success 分支可提供响应骨架 stub.body/status",
                "非 success 分支必须有 source.rule_keys，且只能引用输入规则",
                "无法确定时不要生成分支，写 gaps",
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("mock_semantics_llm_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise AgentError("mock_semantics_invalid", "模型输出必须是 JSON object")
    try:
        draft = MockSemanticDraft.model_validate(payload)
    except Exception as exc:
        raise AgentError("mock_semantics_invalid", str(exc)[:2_000]) from exc
    result = apply_mock_semantic_candidates(
        semantic, draft.model_dump(mode="json"), context=context, ruleset=ruleset
    )
    if not result.ok:
        raise AgentError(
            "mock_semantics_candidates_invalid",
            "；".join(result.errors[:20]),
            details={"errors": result.errors, "warnings": result.warnings},
        )
    return result


__all__ = [
    "MockSemanticDraft",
    "generate_mock_semantic_candidates",
    "load_mock_semantic_prompt",
]
