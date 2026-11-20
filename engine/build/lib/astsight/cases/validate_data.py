"""校验：data。"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from contracts import (
    CaseLevel,
    CaseStep,
    DataCatalog,
    DubboCall,
    MockSemantic,
    PreconditionKind,
    RequirementDoc,
    RuleAuthority,
    RuleSet,
    RuleStatus,
    TestCase,
    Transport,
)
from .context import (
    context_documents,
    context_doc_versions,
    context_included_sections,
    context_requirements,
    find_profile,
    parse_endpoint,
)
from .data_refs import iter_data_refs, lookup_data
from .draft import (
    CATEGORY_RE,
    CASE_KEY_RE,
    MAX_BODY_DEPTH,
    MAX_BODY_ITEMS,
    STEP_NAME_RE,
    CaseDraft,
    DraftCase,
    DraftCaseStep,
    body_depth,
    body_item_count,
    case_fingerprint,
    to_test_case,
)
from .expressions import (
    field_paths,
    has_dynamic,
    is_step_ref,
    iter_dynamic_nodes,
    validate_dynamic,
)
from ..persistence import sha256_json
from .requirements import normalize_text
from .relations import materialize_rule_relations
from .strength import derive_assert_strength

from .validate_base import *


def _case_data_refs(draft_case: DraftCase) -> list[str]:
    """收集请求、断言与 Mock 响应补充里的 `$data` 引用。"""
    values: list[Any] = [
        draft_case.path_params,
        draft_case.query,
        draft_case.body,
        draft_case.mock_replies,
    ]
    if draft_case.dubbo is not None:
        values.extend(
            [draft_case.dubbo.arguments, draft_case.dubbo.attachments]
        )
    for step in draft_case.steps:
        values.extend([step.path_params, step.query, step.body])
        if step.dubbo is not None:
            values.extend([step.dubbo.arguments, step.dubbo.attachments])
        for assertion in step.asserts:
            values.extend([assertion.expect, assertion.where])
    for assertion in draft_case.asserts:
        values.extend([assertion.expect, assertion.where])
    for precondition in draft_case.preconditions:
        values.extend([precondition.where, precondition.set])
    for item in draft_case.mq_assertions:
        values.extend([item.key, item.body])
    refs: list[str] = []
    for value in values:
        refs.extend(iter_data_refs(value))
    return refs


def _check_case_data(
    draft_case: DraftCase,
    *,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    """用例级业务实体：键名、规模、引用可解析，且 Mock 补充有对应绑定。"""
    data = draft_case.data
    if len(data) > _MAX_DATA_KEYS:
        result.errors.append(
            _issue(
                "error",
                "data.too_many_keys",
                f"data 数量 {len(data)} 超过上限 {_MAX_DATA_KEYS}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for key, value in data.items():
        if not _DATA_KEY_RE.match(str(key)):
            result.errors.append(
                _issue(
                    "error",
                    "data.key_invalid",
                    f"data 键必须是小写安全标识: {key!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        nested = list(iter_data_refs(value))
        if nested:
            result.errors.append(
                _issue(
                    "error",
                    "data.nested_ref",
                    f"data[{key!r}] 不能再引用 $data: {nested[:3]}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if (
            body_depth(value) > MAX_BODY_DEPTH
            or body_item_count(value) > MAX_BODY_ITEMS
        ):
            result.errors.append(
                _issue(
                    "error",
                    "data.too_large",
                    f"data[{key!r}] 超过深度/条目上限",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )

    refs = _case_data_refs(draft_case)
    if len(refs) > _MAX_DATA_REFS:
        result.errors.append(
            _issue(
                "error",
                "data.ref_limit",
                f"$data 引用数量 {len(refs)} 超过上限 {_MAX_DATA_REFS}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for ref in refs:
        found, _ = lookup_data(data, ref)
        if not found:
            result.errors.append(
                _issue(
                    "error",
                    "data.ref_unknown",
                    f"$data 引用不存在: {ref!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    used = {ref.split(".", 1)[0] for ref in refs}
    for key in data:
        if key not in used:
            result.warnings.append(
                _issue(
                    "warning",
                    "data.unused",
                    f"data[{key!r}] 没有被请求、断言或 Mock 引用",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )

    for dep_id, reply in draft_case.mock_replies.items():
        if dep_id not in draft_case.mock_bindings:
            result.errors.append(
                _issue(
                    "error",
                    "mock.reply_dep_missing_binding",
                    f"mock_replies 的 dep_id 没有 default mock_binding: {dep_id!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if not isinstance(reply, dict):
            result.errors.append(
                _issue(
                    "error",
                    "mock.reply_invalid",
                    f"mock_replies[{dep_id!r}] 必须是 JSON 对象",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif (
            body_depth(reply) > MAX_BODY_DEPTH
            or body_item_count(reply) > MAX_BODY_ITEMS
        ):
            result.errors.append(
                _issue(
                    "error",
                    "mock.reply_invalid",
                    f"mock_replies[{dep_id!r}] 超过深度/条目上限",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
