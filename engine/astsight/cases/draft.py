"""需求→用例草案模型与解析。

草案是外部 Skill/人工产物，属于不可信输入：文件大小、用例数、断言数、
请求体深度都有上限；`method/path` 由 context 回填，不允许草案自行编造。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts import (
    AssertStrength,
    Assertion,
    CaseLevel,
    CaseStep,
    ChangeRef,
    CoverageRole,
    Precondition,
    Priority,
    RuleRef,
    RuleSet,
    RuleStatus,
    SourceRef,
    TestCase,
    Transport,
)

from ..persistence import sha256_json
from .strength import derive_assert_strength

MAX_DRAFT_BYTES = 5 * 1024 * 1024
MAX_CASES = 500
MAX_ASSERTS_PER_CASE = 50
MAX_ASSERTS_PER_STEP = 50
MAX_STEPS_PER_CASE = 50
MAX_BODY_DEPTH = 20
MAX_BODY_ITEMS = 5000
MAX_RULE_REFS_PER_CASE = 50
MAX_CHANGE_REFS_PER_CASE = 20
MAX_PRECONDITIONS_PER_CASE = 20

CASE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
STEP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class DraftRequirementRef(BaseModel):
    """草案里的需求引用；version 由 context 在 import 时回填。"""

    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="context 中的项目相对文档路径")
    section: str = Field(description="Markdown 标题路径")
    quote: str = Field(description="section 原文片段，必须能在原文中找到")


class DraftCaseStep(BaseModel):
    """多请求用例的一步；`endpoint` 必须能在 context 中定位。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="步骤名，匹配 ^[a-z][a-z0-9_]{1,63}$；用例内唯一"
    )
    endpoint: str = Field(description="METHOD /path")
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(
        default_factory=dict,
        description="Header 值必须是字符串；$ref 会在校验阶段被拒绝",
    )
    body: Any = None
    asserts: list[Assertion] = Field(default_factory=list)


class DraftCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(description="稳定业务标识，如 order_quantity_below_min")
    title: str = Field(default="")
    level: CaseLevel = Field(default=CaseLevel.BUSINESS)
    transport: Transport = Field(default=Transport.HTTP)
    coverage_role: CoverageRole = Field(default=CoverageRole.PRIMARY)
    requirement_refs: list[DraftRequirementRef] = Field(
        default_factory=list,
        max_length=50,
        description="business 用例必填；code 用例可为空",
    )
    rule_refs: list[RuleRef] = Field(
        default_factory=list,
        max_length=MAX_RULE_REFS_PER_CASE,
        description="只写 rule_key/rule_id；authority/source_kind 由 ruleset 回填",
    )
    change_refs: list[ChangeRef] = Field(
        default_factory=list,
        max_length=MAX_CHANGE_REFS_PER_CASE,
        description="code 用例必填，描述变更来源",
    )
    preconditions: list[Precondition] = Field(
        default_factory=list,
        max_length=MAX_PRECONDITIONS_PER_CASE,
    )
    endpoint: str = Field(description="METHOD /path")
    category: str = Field(default="business")
    priority: Priority = Field(default=Priority.P1)
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    asserts: list[Assertion] = Field(default_factory=list)
    steps: list[DraftCaseStep] = Field(
        default_factory=list,
        max_length=MAX_STEPS_PER_CASE,
        description="多请求步骤；非空时按顺序执行，顶层请求/断言必须为空",
    )
    assert_strength: AssertStrength = Field(default=AssertStrength.L1)
    rationale: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    note: str | None = None
    mock_bindings: dict[str, str] = Field(
        default_factory=dict,
        description="可选：dep_id → branch；执行时写入用例级 Mock 会话",
    )


class UncoveredRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str
    section: str
    reason: str = Field(min_length=1)
    rule_keys: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="该 section 下明确未覆盖的业务规则；用于规则级覆盖对账",
    )


class CaseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    context_id: str | None = None
    generator: dict[str, Any] = Field(default_factory=dict)
    ruleset_id: str | None = None
    ruleset_hash: str | None = None
    scope_docs: list[str] = Field(default_factory=list)
    cases: list[DraftCase] = Field(default_factory=list)
    uncovered_requirements: list[UncoveredRequirement] = Field(default_factory=list)


def body_depth(value: Any, depth: int = 0) -> int:
    if isinstance(value, dict):
        if not value:
            return depth + 1
        return max(body_depth(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        if not value:
            return depth + 1
        return max(body_depth(item, depth + 1) for item in value)
    return depth + 1


def body_item_count(value: Any) -> int:
    if isinstance(value, dict):
        return len(value) + sum(body_item_count(item) for item in value.values())
    if isinstance(value, list):
        return len(value) + sum(body_item_count(item) for item in value)
    return 1


def load_draft(path: str | Path) -> CaseDraft:
    """读取并做资源上限检查；结构错误交给 Pydantic 抛出。"""
    draft_path = Path(path)
    if not draft_path.is_file():
        raise FileNotFoundError(f"草案不存在: {draft_path}")
    size = draft_path.stat().st_size
    if size > MAX_DRAFT_BYTES:
        raise ValueError(
            f"草案文件过大: {size} bytes > {MAX_DRAFT_BYTES} bytes"
        )
    raw = draft_path.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    draft = CaseDraft.model_validate(data)
    if len(draft.cases) > MAX_CASES:
        raise ValueError(f"草案用例数过多: {len(draft.cases)} > {MAX_CASES}")
    for case in draft.cases:
        if len(case.asserts) > MAX_ASSERTS_PER_CASE:
            raise ValueError(
                f"{case.case_key} 断言过多: {len(case.asserts)} > {MAX_ASSERTS_PER_CASE}"
            )
        if len(case.steps) > MAX_STEPS_PER_CASE:
            raise ValueError(
                f"{case.case_key} 步骤过多: {len(case.steps)} > {MAX_STEPS_PER_CASE}"
            )
        bodies = [case.body] + [step.body for step in case.steps]
        for body in bodies:
            depth = body_depth(body)
            if depth > MAX_BODY_DEPTH:
                raise ValueError(
                    f"{case.case_key} 请求体嵌套过深: {depth} > {MAX_BODY_DEPTH}"
                )
            items = body_item_count(body)
            if items > MAX_BODY_ITEMS:
                raise ValueError(
                    f"{case.case_key} 请求体元素过多: {items} > {MAX_BODY_ITEMS}"
                )
        for step in case.steps:
            if len(step.asserts) > MAX_ASSERTS_PER_STEP:
                raise ValueError(
                    f"{case.case_key}.{step.name} 断言过多: "
                    f"{len(step.asserts)} > {MAX_ASSERTS_PER_STEP}"
                )
    return draft


def draft_fingerprint(draft: CaseDraft) -> str:
    return sha256_json(draft.model_dump(mode="json"))


def case_anchor(endpoint: str, case_key: str) -> str:
    return f"{endpoint}\u0000{case_key}"


def proposal_key(endpoint: str, case_key: str) -> str:
    return f"{endpoint}::{case_key}"


def _source_for(refs: list[SourceRef], generator: dict[str, Any]) -> str:
    if refs:
        first = refs[0]
        if first.doc and first.section:
            return f"requirement:{first.doc}#{first.section}"
    kind = str(generator.get("kind") or "manual")
    name = str(generator.get("name") or kind)
    return f"{kind}:{name}"


def resolve_rule_refs(
    refs: list[RuleRef], ruleset: RuleSet | None
) -> list[RuleRef]:
    """以规则库为事实源回填 authority/source_kind，不信任草案自报。"""
    if ruleset is None:
        return [ref.model_copy() for ref in refs]
    index = ruleset.rule_index()
    resolved: list[RuleRef] = []
    for ref in refs:
        rule = index.get(ref.rule_key)
        if rule is None:
            resolved.append(ref.model_copy())
            continue
        source = getattr(rule, "source", None)
        source_kind = getattr(source, "kind", ref.source_kind)
        resolved.append(
            RuleRef(
                rule_key=rule.rule_key,
                rule_id=rule.rule_id,
                authority=rule.authority,
                source_kind=source_kind,
            )
        )
    return resolved


def to_test_case(
    draft_case: DraftCase,
    *,
    method: str,
    path: str,
    doc_versions: dict[str, str],
    generator: dict[str, Any],
    case_id: str = "TC-0000",
    ruleset: RuleSet | None = None,
    steps: list[CaseStep] | None = None,
) -> TestCase:
    """把草案用例解析成执行形态；version 取自 context 文档 hash。"""
    refs = [
        SourceRef(
            doc=ref.doc,
            section=ref.section,
            quote=ref.quote,
            version=doc_versions.get(ref.doc),
        )
        for ref in draft_case.requirement_refs
    ]
    effective_steps = steps if steps is not None else draft_case.steps
    all_asserts = [
        *draft_case.asserts,
        *(assertion for step in effective_steps for assertion in step.asserts),
    ]
    return TestCase(
        id=case_id,
        title=draft_case.title or draft_case.case_key,
        level=draft_case.level,
        transport=draft_case.transport,
        coverage_role=draft_case.coverage_role,
        rule_refs=resolve_rule_refs(draft_case.rule_refs, ruleset),
        change_refs=[ref.model_copy() for ref in draft_case.change_refs],
        preconditions=[item.model_copy() for item in draft_case.preconditions],
        endpoint=draft_case.endpoint,
        category=draft_case.category,
        method=method.upper(),
        path=path,
        priority=draft_case.priority,
        requirement_refs=refs,
        rationale=draft_case.rationale,
        path_params=draft_case.path_params,
        query=draft_case.query,
        headers=draft_case.headers,
        body=draft_case.body,
        asserts=draft_case.asserts,
        steps=[step.model_copy(deep=True) for step in (steps or [])],
        source=_source_for(refs, generator),
        assert_strength=derive_assert_strength(
            all_asserts, steps=len(effective_steps)
        ),
        note=draft_case.note,
        mock_bindings=dict(draft_case.mock_bindings),
    )


def _assert_payload(assertion: Assertion) -> dict[str, Any]:
    """指纹里的断言投影；where 为空时不写入，保持旧用例指纹不变。"""
    payload = {
        "kind": assertion.kind,
        "target": assertion.target,
        "op": assertion.op,
        "expect": assertion.expect,
    }
    if assertion.where:
        payload["where"] = assertion.where
    return payload


def case_fingerprint(case: TestCase, case_key: str) -> str:
    """执行行为 + 需求归属的稳定指纹。

    排除 id/source/title/note/rationale/version/审批信息；文档 hash 也不参与，
    否则文档任何无关改动都会触发重新审核。
    """
    refs = sorted(
        (
            {
                "doc": ref.doc or "",
                "section": ref.section or "",
                "quote": ref.quote or "",
            }
            for ref in case.requirement_refs
        ),
        key=lambda item: (item["doc"], item["section"], item["quote"]),
    )
    payload = {
        "case_key": case_key,
        "level": case.level.value,
        "transport": case.transport.value,
        "coverage_role": case.coverage_role.value,
        "endpoint": case.endpoint,
        "category": case.category,
        "priority": case.priority.value,
        "method": case.method.upper(),
        "path": case.path,
        "path_params": case.path_params,
        "query": case.query,
        "headers": {str(k).lower(): v for k, v in case.headers.items()},
        "body": case.body,
        "asserts": [_assert_payload(assertion) for assertion in case.asserts],
        "assert_strength": case.assert_strength.value,
        # 为空时不写入，保持既有单请求用例指纹不变。
        **(
            {
                "steps": [
                    {
                        "name": step.name,
                        "endpoint": step.endpoint,
                        "method": step.method.upper(),
                        "path": step.path,
                        "path_params": step.path_params,
                        "query": step.query,
                        "headers": {
                            str(k).lower(): v for k, v in step.headers.items()
                        },
                        "body": step.body,
                        "asserts": [
                            _assert_payload(assertion) for assertion in step.asserts
                        ],
                    }
                    for step in case.steps
                ]
            }
            if case.steps
            else {}
        ),
        "requirement_refs": refs,
        "rule_keys": sorted({ref.rule_key for ref in case.rule_refs}),
        "preconditions": [
            {
                "kind": item.kind.value,
                "state_key": item.state_key,
                "table": item.table or "",
                "where": item.where,
                "set": item.set,
            }
            for item in case.preconditions
        ],
        "mock_bindings": dict(sorted(case.mock_bindings.items())),
    }
    # change_refs 不进指纹：换变更分支不应触发业务用例重审。
    return sha256_json(payload)


def draft_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


__all__ = [
    "CASE_KEY_RE",
    "CATEGORY_RE",
    "MAX_STEPS_PER_CASE",
    "STEP_NAME_RE",
    "CaseDraft",
    "DraftCase",
    "DraftCaseStep",
    "DraftRequirementRef",
    "UncoveredRequirement",
    "body_depth",
    "body_item_count",
    "case_anchor",
    "case_fingerprint",
    "draft_bytes",
    "draft_fingerprint",
    "load_draft",
    "proposal_key",
    "resolve_rule_refs",
    "to_test_case",
]
