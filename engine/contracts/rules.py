"""规则库契约（C1/A1）。

规则是设计期资产：A1 只抽取业务规则（BR），契约规则（CR）由框架从
OpenAPI/画像确定性提取。`rule_key` 与 fingerprint 都由框架计算，不能信任模型。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    CheckDimension,
    ConflictKind,
    ConflictSeverity,
    Priority,
    RuleAuthority,
    RuleOperator,
    RuleSourceKind,
    RuleStatus,
    RuleType,
    Transport,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class RuleSource(BaseModel):
    """规则来源；`section` 是稳定路径，`quote` 必须能在原文中找到。"""

    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="项目相对文档路径")
    section: str = Field(description="Markdown 标题路径/章节路径")
    kind: RuleSourceKind = Field(default=RuleSourceKind.EXPLICIT)
    version: str | None = Field(default=None, description="文档 sha256")
    doc_version: str | None = Field(default=None, description="可选文档版本号")
    quote: str | None = Field(default=None, description="规则原文片段")


class RuleConstraint(BaseModel):
    """规则中的结构化约束。

    只用于确定性比较（grounding/冲突检测），不是运行时可执行表达式。
    `values` 统一为字符串，避免模型输出 `1` 与 `"1"` 造成比较噪音。
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(default="", max_length=200, description="字段/对象，如 body.quantity")
    operator: RuleOperator = Field(default=RuleOperator.OTHER)
    values: list[str] = Field(default_factory=list, max_length=20)
    condition: str = Field(default="", max_length=500, description="同一条件才比较冲突")
    error_code: str = Field(default="", max_length=100)
    exhaustive: bool | None = Field(
        default=None,
        description=(
            "operator=in/not_in 时，values 是否为完整集合。"
            "None 表示未声明（框架记录 warning，无法判断枚举是否闭合）；"
            "False 表示确认不完整。二者不可混同。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _stringify_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        values = data.get("values")
        if values is None:
            data["values"] = []
        elif isinstance(values, (str, int, float)):
            data["values"] = [str(values)]
        else:
            data["values"] = [str(value) for value in values]
        if data.get("error_code") is not None:
            data["error_code"] = str(data["error_code"])
        return data

    def content_payload(self) -> dict[str, Any]:
        return {
            "field": _norm(self.field),
            "operator": self.operator.value,
            "values": [_norm(value) for value in self.values],
            "condition": _norm(self.condition),
            "error_code": _norm(self.error_code),
            # None 与 False 在指纹上等价：未声明不等于约束变了，
            # 否则补一次 exhaustive 声明就会让所有相关规则 fingerprint 漂移。
            "exhaustive": bool(self.exhaustive),
        }


class Rule(BaseModel):
    """一条业务规则（BR）。"""

    model_config = ConfigDict(extra="forbid")

    rule_key: str = Field(default="", description="框架计算：RK-<sha256>[:12]")
    rule_id: str = Field(default="", description="展示编号 BR-NN")
    text: str = Field(min_length=1, max_length=500)
    type: RuleType = Field(default=RuleType.BUSINESS_CONSTRAINT)
    entities: list[str] = Field(default_factory=list, max_length=50)
    fields: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[RuleConstraint] = Field(default_factory=list, max_length=50)
    source: RuleSource
    authority: RuleAuthority = Field(default=RuleAuthority.B)
    impact: str = Field(default="", max_length=1_000)
    priority: Priority = Field(default=Priority.P1)
    reasoning: str = Field(default="", max_length=2_000)
    fingerprint: str = Field(default="")
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)

    def content_payload(self) -> dict[str, Any]:
        payload = {
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
        # 空约束不加 key：兼容加结构化约束之前保存的 ruleset fingerprint。
        if self.constraints:
            payload["constraints"] = sorted(
                (item.content_payload() for item in self.constraints),
                key=_canonical,
            )
        return payload


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
    source: str = Field(
        default="",
        max_length=300,
        description="出处（doc#section）；人工确认时据此回查原文",
    )
    related_rule_keys: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="该问题阻塞的规则；不回答则这些规则没有判定基准",
    )


class RuleGap(BaseModel):
    """规则缺口；`reason` 必填，避免只有空泛的 gap 名。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="implicit", max_length=64)
    desc: str = Field(min_length=1, max_length=1_000)
    reason: str = Field(min_length=1, max_length=1_000)
    related_interfaces: list[str] = Field(default_factory=list, max_length=50)
    check_dimension: CheckDimension | None = Field(
        default=None,
        description="覆盖查漏的 12 维度之一；为空说明这条 gap 不是按维度查出来的",
    )
    affected_rule_keys: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="受该缺口影响的规则；回写需求后据此算影响面",
    )


class RuleConflict(BaseModel):
    """规则冲突记录。

    `kind` 决定处置路径而不是分类标签：semantic / cross_section / value /
    unreachable 这几类只靠文档内部就能判定，属于缺陷，不需要业务方回答；
    其余类型同理由规则组合推出。规则阶段不设人工裁决，冲突只作为证据保留，
    在 A4 输入与用例审核文件中展示，由用例审核这一步统一确认。
    """

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=200)
    kind: ConflictKind = Field(
        default=ConflictKind.OTHER,
        description="冲突类型；决定是否需要人回答",
    )
    severity: ConflictSeverity | None = Field(
        default=None,
        description="后果；用于排序，不作为结论",
    )
    rule_keys: list[str] = Field(default_factory=list, max_length=20)
    docs: list[str] = Field(default_factory=list, max_length=20)
    origin: str = Field(
        default="model",
        description="model / framework / model+framework",
    )
    reason: str = Field(default="", max_length=2_000)


class RuleSet(BaseModel):
    """项目级规则库 `ruleset.json`。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
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
    "RuleConstraint",
    "RuleGap",
    "RuleQuestion",
    "RuleSet",
    "RuleSource",
]
