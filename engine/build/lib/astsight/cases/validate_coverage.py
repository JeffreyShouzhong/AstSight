"""校验：coverage。"""
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


def _check_requirement_refs(
    draft_case: DraftCase,
    *,
    documents: dict[str, dict[str, Any]],
    explicit_scope: set[str],
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
    scope_docs: set[str],
) -> None:
    for ref in draft_case.requirement_refs:
        scope_docs.add(ref.doc)
        if explicit_scope and ref.doc not in explicit_scope:
            result.warnings.append(
                _issue(
                    "warning",
                    "scope.doc_auto_added",
                    f"引用了显式 scope 之外的文档，已自动纳入本次 scope: {ref.doc}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        document = documents.get(ref.doc)
        if document is None:
            result.errors.append(
                _issue(
                    "error",
                    "requirement.doc_unknown",
                    f"需求文档不在 context 中: {ref.doc}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
            continue
        sections = {
            str(section["section"]): section
            for section in document.get("sections") or []
        }
        section = sections.get(ref.section)
        if section is None:
            result.errors.append(
                _issue(
                    "error",
                    "requirement.section_unknown",
                    f"需求 section 不存在: {ref.doc}#{ref.section}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
            continue
        if not section.get("include", True):
            result.warnings.append(
                _issue(
                    "warning",
                    "requirement.metadata_section",
                    f"引用的是元数据 section，不计入覆盖率: {ref.doc}#{ref.section}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        quote = normalize_text(ref.quote)
        body = normalize_text(str(section.get("text") or ""))
        if len(quote) < 4:
            result.errors.append(
                _issue(
                    "error",
                    "requirement.quote_too_short",
                    f"quote 至少 4 个非空白字符: {ref.quote!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
            continue
        occurrences = body.count(quote)
        if occurrences == 0:
            result.errors.append(
                _issue(
                    "error",
                    "requirement.quote_not_found",
                    f"quote 不在 section 原文中: {ref.doc}#{ref.section}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif occurrences > 1:
            result.warnings.append(
                _issue(
                    "warning",
                    "requirement.quote_ambiguous",
                    f"quote 在 section 中出现 {occurrences} 次: {ref.quote!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )


def _check_rule_coverage(
    draft: CaseDraft,
    *,
    ruleset: RuleSet | None,
    result: ValidationResult,
) -> None:
    """A/B 级业务规则必须有用例引用，或在 uncovered 里显式声明。

    `section` 级声明是向后兼容的弱口径：旧草案没有 `rule_keys` 时，
    只要该 section 已声明未覆盖就不重复告警。新草案应写 `rule_keys`。
    """
    if ruleset is None:
        return
    referenced = {
        ref.rule_key
        for resolved in result.resolved
        for ref in resolved.case.rule_refs
    }
    declared = {
        key
        for item in draft.uncovered_requirements
        for key in item.rule_keys
    }
    declared_sections = {
        (item.doc, item.section) for item in draft.uncovered_requirements
    }
    for rule in ruleset.rules:
        if rule.status is not RuleStatus.ACTIVE:
            continue
        if rule.authority not in {RuleAuthority.A, RuleAuthority.B}:
            continue
        if rule.rule_key in referenced or rule.rule_key in declared:
            continue
        if (rule.source.doc, rule.source.section) in declared_sections:
            continue
        result.warnings.append(
            _issue(
                "warning",
                "rule.uncovered",
                f"规则未被任何用例引用，也未声明未覆盖: "
                f"{rule.rule_id} ({rule.rule_key})",
                path="uncovered_requirements",
            )
        )


def _check_relation_coverage(
    draft: CaseDraft,
    *,
    ruleset: RuleSet | None,
    result: ValidationResult,
) -> None:
    if ruleset is None:
        return
    relation_index = {
        relation.relation_key: rule
        for rule in ruleset.rules
        for relation in rule.relations
        if relation.relation_key
    }
    covered: set[str] = set()
    for resolved in result.resolved:
        for assertion in [
            *resolved.case.asserts,
            *(
                assertion
                for step in resolved.case.steps
                for assertion in step.asserts
            ),
        ]:
            if not assertion.relation_ref:
                continue
            if assertion.relation_ref not in relation_index:
                result.errors.append(
                    _issue(
                        "error",
                        "relation.unknown",
                        f"断言引用未知 relation_key: {assertion.relation_ref}",
                        case_key=resolved.case_key,
                        endpoint=resolved.endpoint,
                    )
                )
            else:
                covered.add(assertion.relation_ref)
    declared = {
        key
        for item in draft.uncovered_requirements
        for key in item.relation_keys
    }
    for key in sorted(declared - set(relation_index)):
        result.errors.append(
            _issue(
                "error",
                "relation.uncovered_unknown",
                f"uncovered_requirements 引用未知 relation_key: {key}",
                path="uncovered_requirements",
            )
        )
    for key in sorted(declared & covered):
        result.warnings.append(
            _issue(
                "warning",
                "relation.declared_but_covered",
                f"关系已生成断言，却仍声明未覆盖: {key}",
                path="uncovered_requirements",
            )
        )
    for key, rule in relation_index.items():
        if rule.status is not RuleStatus.ACTIVE:
            continue
        if rule.authority not in {RuleAuthority.A, RuleAuthority.B}:
            continue
        if key in covered or key in declared:
            continue
        result.errors.append(
            _issue(
                "error",
                "relation.unaccounted",
                f"规则关系未被用例物化，也未声明未覆盖: "
                f"{rule.rule_id} / {key}",
                path="uncovered_requirements",
            )
        )
