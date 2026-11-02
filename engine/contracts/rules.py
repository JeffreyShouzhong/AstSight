"""规则库契约（C1/A1）。

规则是设计期资产：A1 只抽取业务规则（BR），契约规则（CR）由框架从
OpenAPI/画像确定性提取。`rule_key` 与 fingerprint 都由框架计算，不能信任模型。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import Priority, RuleAuthority, RuleSourceKind, RuleStatus, RuleType, Transport


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class RuleSource(BaseModel):
    """规则来源；`section` 是稳定路径，`quote` 必须能在原文中找到。"""

    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="项目相对文档路径")
    section: str = Field(description="Markdown 标题路径/章节路径")
    kind: RuleSourceKind = Field(default=RuleSourceKind.EXPLICIT)
    version: str | None = Field(default=None, description="文档 sha256")
    doc_version: str | None = Field(default=None, description="可选文档版本号")
    quote: str | None = Field(default=None, description="规则原文片段")


class Rule(BaseModel):
    """一条业务规则（BR）。"""

    model_config = ConfigDict(extra="forbid")

    rule_key: str = Field(default="", description="框架计算：RK-<sha256>[:12]")
    rule_id: str = Field(default="", description="展示编号 BR-NN")
    text: str = Field(min_length=1, max_length=500)
    type: RuleType = Field(default=RuleType.BUSINESS_CONSTRAINT)
    entities: list[str] = Field(default_factory=list, max_length=50)
    fields: list[str] = Field(default_factory=list, max_length=50)
    source: RuleSource
    authority: RuleAuthority = Field(default=RuleAuthority.B)
    impact: str = Field(default="", max_length=1_000)
    priority: Priority = Field(default=Priority.P1)
    reasoning: str = Field(default="", max_length=2_000)
    fingerprint: str = Field(default="")
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)

    def content_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "type": self.type.value,
            "fields": sorted(self.fields),
            "source": {
                "doc": self.source.doc,
                "section": self.source.section,
                "kind": self.source.kind.value,
            },
            "authority": self.authority.value,
            "priority": self.priority.value,
        }


class ContractRule(BaseModel):
    """一条契约规则（CR），由 OpenAPI/画像确定性提取。"""

    model_config = ConfigDict(extra="forbid")

    rule_key: str = Field(default="")
    rule_id: str = Field(default="")
    transport: Transport = Field(default=Transport.HTTP)
    endpoint: str = Field(description="METHOD /path")
    field: str = Field(default="", max_length=200)
    constraint: str = Field(min_length=1, max_length=1_000)
    source: RuleSource | None = Field(default=None)
    authority: RuleAuthority = Field(default=RuleAuthority.A)
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)

    def content_payload(self) -> dict[str, Any]:
        return {
            "transport": self.transport.value,
            "endpoint": self.endpoint,
            "field": self.field,
            "constraint": self.constraint,
        }


class RuleQuestion(BaseModel):
    """A1 无法从文档确认、需要人工回答的问题。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="", max_length=64)
    text: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(default="", max_length=1_000)
    impact: str = Field(default="", max_length=1_000)


class RuleGap(BaseModel):
    """规则缺口；`reason` 必填，避免只有空泛的 gap 名。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="implicit", max_length=64)
    desc: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(min_length=1, max_length=1_000)
    related_interfaces: list[str] = Field(default_factory=list, max_length=50)


class RuleConflict(BaseModel):
    """规则冲突；未裁决时相关规则应保持 disputed。"""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=200)
    rule_keys: list[str] = Field(default_factory=list, max_length=20)
    docs: list[str] = Field(default_factory=list, max_length=20)
    decision: str = Field(default="", max_length=2_000)
    resolved_by: str | None = Field(default=None)
    resolved_at: str | None = Field(default=None)


class RuleSet(BaseModel):
    """项目级规则库 `ruleset.json`。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    ruleset_id: str = Field(default="")
    project: str = Field(default="")
    version: int = Field(default=1, ge=1)
    generated_at: str = Field(default="")
    prompt: dict[str, Any] = Field(default_factory=dict)
    documents: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    contract_rules: list[ContractRule] = Field(default_factory=list)
    questions: list[RuleQuestion] = Field(default_factory=list)
    gaps: list[RuleGap] = Field(default_factory=list)
    conflicts: list[RuleConflict] = Field(default_factory=list)

    def all_rules(self) -> list[Rule | ContractRule]:
        return [*self.rules, *self.contract_rules]

    def rule_index(self) -> dict[str, Rule | ContractRule]:
        return {rule.rule_key: rule for rule in self.all_rules() if rule.rule_key}

    def content_hash(self) -> str:
        return _hash(self.model_dump(mode="json"))


__all__ = [
    "ContractRule",
    "Rule",
    "RuleConflict",
    "RuleGap",
    "RuleQuestion",
    "RuleSet",
    "RuleSource",
]
