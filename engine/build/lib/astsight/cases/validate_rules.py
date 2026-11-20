"""校验：rules。"""
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


def _check_grounding(
    assertions: list[Any],
    requirement_refs: list[Any],
    *,
    case_key: str,
    endpoint: str,
    profile: dict[str, Any],
    path: str,
    result: ValidationResult,
) -> None:
    expected: list[int] = []
    for assertion in assertions:
        if assertion.kind == "status_code":
            expected.extend(_expected_status_codes(assertion))
    non_2xx = [code for code in expected if code >= 400]
    if not non_2xx:
        return
    # OpenAPI 中显式声明的错误响应是合法依据。
    declared = {
        int(code)
        for code in (profile.get("error_codes") or [])
        if isinstance(code, int) or str(code).isdigit()
    }
    if any(code in declared for code in non_2xx):
        return
    quotes = " ".join(ref.quote or "" for ref in requirement_refs)
    if not _grounded_in_quote(non_2xx, quotes):
        result.warnings.append(
            _issue(
                "warning",
                "assertion.not_grounded",
                f"非 2xx 期望 {non_2xx} 未在需求 quote/OpenAPI 错误响应中找到依据",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )


def _check_rule_refs(
    draft_case: DraftCase,
    *,
    ruleset: RuleSet | None,
    require_rules: bool,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    if require_rules and ruleset is None:
        result.errors.append(
            _issue(
                "error",
                "ruleset.missing",
                "该流程要求提供 ruleset，但未加载规则库",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if require_rules and not draft_case.rule_refs:
        result.errors.append(
            _issue(
                "error",
                "case.rule_refs_missing",
                "规则驱动流程的用例至少引用一条规则",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if not draft_case.rule_refs:
        return
    if ruleset is None:
        result.warnings.append(
            _issue(
                "warning",
                "ruleset.missing",
                "未提供 ruleset，无法核对 rule_refs",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    index = ruleset.rule_index()
    conflict_keys = {
        key for conflict in ruleset.conflicts for key in conflict.rule_keys
    }
    authorities: list[RuleAuthority] = []
    for ref in draft_case.rule_refs:
        rule = index.get(ref.rule_key)
        if rule is None:
            result.errors.append(
                _issue(
                    "error",
                    "rule.unknown",
                    f"rule_key 不在规则库中: {ref.rule_key!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
            continue
        authorities.append(rule.authority)
        if ref.rule_key in conflict_keys:
            result.warnings.append(
                _issue(
                    "warning",
                    "rule.conflict",
                    f"规则参与未解决的冲突，审核时需确认: "
                    f"{rule.rule_id} ({rule.rule_key})",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if rule.status is RuleStatus.UNGROUNDED:
            result.errors.append(
                _issue(
                    "error",
                    "rule.ungrounded",
                    f"规则无原文依据: {rule.rule_id} ({rule.rule_key})",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif rule.status is RuleStatus.DEPRECATED:
            result.errors.append(
                _issue(
                    "error",
                    "rule.deprecated",
                    f"规则已废弃: {rule.rule_id} ({rule.rule_key})",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    if authorities and all(
        authority in {RuleAuthority.C, RuleAuthority.D} for authority in authorities
    ):
        result.errors.append(
            _issue(
                "error",
                "rule.authority_insufficient",
                "C/D 级规则不能作为用例的唯一依据",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )


def _check_level_refs(
    draft_case: DraftCase,
    *,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    if draft_case.level is CaseLevel.BUSINESS:
        if not draft_case.requirement_refs:
            result.errors.append(
                _issue(
                    "error",
                    "case.requirement_refs_missing",
                    "business 用例至少引用一条需求原文",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    else:
        if not draft_case.change_refs:
            result.errors.append(
                _issue(
                    "error",
                    "case.change_refs_missing",
                    "code 用例至少引用一条变更来源",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )


def _check_assert_strength(
    draft_case: DraftCase,
    *,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    derived = derive_assert_strength(
        [
            *draft_case.asserts,
            *(
                assertion
                for step in draft_case.steps
                for assertion in step.asserts
            ),
        ],
        steps=len(draft_case.steps) or 1,
    )
    if draft_case.assert_strength != derived:
        result.warnings.append(
            _issue(
                "warning",
                "assert_strength.mismatch",
                f"声明 {draft_case.assert_strength.value}，按断言内容推导为 "
                f"{derived.value}；导入以推导值为准",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
