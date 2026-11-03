"""A1 模型输出契约（不可信输入）。"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from contracts import (
    CheckDimension,
    ConflictKind,
    ConflictSeverity,
    Priority,
    RuleAuthority,
    RuleConstraint,
    RuleSourceKind,
    RuleType,
)

MAX_RULE_DRAFT_BYTES = 5 * 1024 * 1024
MAX_RULES = 500
MAX_RULE_TEXT = 500


class DraftRuleSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="context 中的项目相对文档路径")
    section: str | None = Field(default=None, description="标题路径")
    number: str | None = Field(default=None, description="可读章节号")
    quote: str = Field(default="", description="section 原文片段")
    kind: RuleSourceKind = Field(default=RuleSourceKind.EXPLICIT)
    doc_version: str | None = None


class DraftRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str | None = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=MAX_RULE_TEXT)
    type: RuleType = Field(default=RuleType.BUSINESS_CONSTRAINT)
    entities: list[str] = Field(default_factory=list, max_length=50)
    fields: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[RuleConstraint] = Field(default_factory=list, max_length=50)
    source: DraftRuleSource
    authority: RuleAuthority = Field(default=RuleAuthority.B)
    impact: str = Field(default="", max_length=1_000)
    priority: Priority = Field(default=Priority.P1)
    reasoning: str = Field(default="", max_length=2_000)


class DraftQuestion(BaseModel):
    """需要人工回答的问题；答案只能来自业务决策或文档之外的事实。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="", max_length=64)
    text: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(default="", max_length=1_000)
    impact: str = Field(default="", max_length=1_000)
    source: str = Field(
        default="",
        max_length=300,
        description="出处（doc#section）；留空时人工确认需回翻全文",
    )
    related_rule_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="该问题会阻塞哪些规则；用于算清不回答的代价",
    )


class DraftGap(BaseModel):
    """文档缺口；`check_dimension` 为空表示这条 gap 不是查漏产出的。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="implicit", max_length=64)
    desc: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(min_length=1, max_length=1_000)
    related_interfaces: list[str] = Field(default_factory=list, max_length=50)
    check_dimension: CheckDimension | None = Field(
        default=None,
        description="覆盖查漏的 12 维度之一；为空说明未按维度检查",
    )
    affected_rule_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="该缺口影响哪些规则；回写需求后据此算影响面",
    )


class DraftConflict(BaseModel):
    """冲突记录；kind 决定处置路径，severity 决定排序。"""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=200)
    kind: ConflictKind = Field(
        default=ConflictKind.OTHER,
        description="冲突类型；semantic/cross_section/value 属可直接判定的缺陷",
    )
    severity: ConflictSeverity | None = Field(
        default=None,
        description="后果；为空表示模型无法判断影响面",
    )
    rule_ids: list[str] = Field(default_factory=list, max_length=20)
    rule_keys: list[str] = Field(default_factory=list, max_length=20)
    docs: list[str] = Field(default_factory=list, max_length=20)
    decision: str = Field(default="", max_length=2_000)


class RuleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    context_id: str | None = None
    summary: str = Field(default="", max_length=8_000)
    rules: list[DraftRule] = Field(default_factory=list, max_length=MAX_RULES)
    questions: list[DraftQuestion] = Field(default_factory=list, max_length=100)
    gaps: list[DraftGap] = Field(default_factory=list, max_length=200)
    conflicts: list[DraftConflict] = Field(default_factory=list, max_length=100)


def load_rule_draft(path: str | Path) -> RuleDraft:
    draft_path = Path(path)
    if not draft_path.is_file():
        raise FileNotFoundError(f"规则草案不存在: {draft_path}")
    if draft_path.stat().st_size > MAX_RULE_DRAFT_BYTES:
        raise ValueError(f"规则草案过大: {draft_path.stat().st_size} bytes")
    data = json.loads(draft_path.read_text(encoding="utf-8-sig"))
    return RuleDraft.model_validate(data)


__all__ = [
    "DraftConflict",
    "DraftGap",
    "DraftQuestion",
    "DraftRule",
    "DraftRuleSource",
    "RuleDraft",
    "load_rule_draft",
]
