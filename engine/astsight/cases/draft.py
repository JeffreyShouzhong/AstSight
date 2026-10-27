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

from contracts import AssertStrength, Assertion, Priority, SourceRef, TestCase

from ..persistence import sha256_json

MAX_DRAFT_BYTES = 5 * 1024 * 1024
MAX_CASES = 500
MAX_ASSERTS_PER_CASE = 50
MAX_BODY_DEPTH = 20
MAX_BODY_ITEMS = 5000

CASE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class DraftRequirementRef(BaseModel):
    """草案里的需求引用；version 由 context 在 import 时回填。"""

    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="context 中的项目相对文档路径")
    section: str = Field(description="Markdown 标题路径")
    quote: str = Field(description="section 原文片段，必须能在原文中找到")


class DraftCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(description="稳定业务标识，如 order_quantity_below_min")
    title: str = Field(default="")
    requirement_refs: list[DraftRequirementRef] = Field(min_length=1)
    endpoint: str = Field(description="METHOD /path")
    category: str = Field(default="business")
    priority: Priority = Field(default=Priority.P1)
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    asserts: list[Assertion] = Field(min_length=1)
    assert_strength: AssertStrength = Field(default=AssertStrength.L1)
    rationale: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    note: str | None = None


class UncoveredRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str
    section: str
    reason: str = Field(min_length=1)


class CaseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    context_id: str | None = None
    generator: dict[str, Any] = Field(default_factory=dict)
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
        depth = body_depth(case.body)
        if depth > MAX_BODY_DEPTH:
            raise ValueError(
                f"{case.case_key} 请求体嵌套过深: {depth} > {MAX_BODY_DEPTH}"
            )
        items = body_item_count(case.body)
        if items > MAX_BODY_ITEMS:
            raise ValueError(
                f"{case.case_key} 请求体元素过多: {items} > {MAX_BODY_ITEMS}"
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


def to_test_case(
    draft_case: DraftCase,
    *,
    method: str,
    path: str,
    doc_versions: dict[str, str],
    generator: dict[str, Any],
    case_id: str = "TC-0000",
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
    return TestCase(
        id=case_id,
        title=draft_case.title or draft_case.case_key,
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
        source=_source_for(refs, generator),
        assert_strength=draft_case.assert_strength,
        note=draft_case.note,
    )


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
        "endpoint": case.endpoint,
        "category": case.category,
        "priority": case.priority.value,
        "method": case.method.upper(),
        "path": case.path,
        "path_params": case.path_params,
        "query": case.query,
        "headers": {str(k).lower(): v for k, v in case.headers.items()},
        "body": case.body,
        "asserts": [
            {
                "kind": assertion.kind,
                "target": assertion.target,
                "op": assertion.op,
                "expect": assertion.expect,
            }
            for assertion in case.asserts
        ],
        "assert_strength": case.assert_strength.value,
        "requirement_refs": refs,
    }
    return sha256_json(payload)


def draft_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


__all__ = [
    "CASE_KEY_RE",
    "CATEGORY_RE",
    "CaseDraft",
    "DraftCase",
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
    "to_test_case",
]
