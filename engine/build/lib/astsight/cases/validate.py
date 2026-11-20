"""草案校验门面：保持 validate_draft 接口与公开模型不变。"""
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
from .validate_assertions import (
    _check_assert, _check_dubbo_assert, _check_schema_assert, _check_db_assert, _hydrate_schema_asserts,
)

from .validate_steps import (
    _check_step_refs, _resolve_case_steps, _check_request,
)

from .validate_data import (
    _case_data_refs, _check_case_data,
)

from .validate_mqmock import (
    _check_mq_assertions, _check_mock_bindings,
)

from .validate_rules import (
    _check_grounding, _check_rule_refs, _check_level_refs, _check_assert_strength,
)

from .validate_dubbo import (
    _hydrate_dubbo, _hydrate_dubbo_call, _check_dubbo_call_fields, _check_dubbo_call,
)

from .validate_profile import (
    _check_profile_alignment, _check_preconditions,
)

from .validate_semantics import (
    _check_mock_semantics,
)

from .validate_coverage import (
    _check_requirement_refs, _check_rule_coverage, _check_relation_coverage,
)


def validate_draft(
    draft: CaseDraft,
    context: dict[str, Any],
    *,
    project: str | Any,
    check_doc_hashes: bool = True,
    ruleset: RuleSet | None = None,
    data_catalog: DataCatalog | None = None,
    mock_semantic: MockSemantic | None = None,
    require_rules: bool = False,
) -> ValidationResult:
    result = ValidationResult(
        context_id=str(context.get("context_id") or ""),
        doc_versions=context_doc_versions(context),
        requirements=context_requirements(context),
    )
    documents = context_documents(context)
    contracts = _dep_contracts(context)
    context_id = str(context.get("context_id") or "")
    if draft.context_id and draft.context_id != context_id:
        result.errors.append(
            _issue(
                "error",
                "draft.context_id_mismatch",
                f"草案 context_id={draft.context_id!r} 与 context={context_id!r} 不一致",
            )
        )
    if draft.schema_version != 1:
        result.errors.append(
            _issue(
                "error",
                "draft.schema_version",
                f"不支持的草案 schema_version={draft.schema_version}",
            )
        )
    materialized = materialize_rule_relations(draft, ruleset)
    draft = materialized.draft
    for message in materialized.errors:
        result.errors.append(
            _issue("error", "relation.materialization_failed", message)
        )
    if ruleset is not None:
        if draft.ruleset_id and draft.ruleset_id != ruleset.ruleset_id:
            result.errors.append(
                _issue(
                    "error",
                    "draft.ruleset_id_mismatch",
                    f"草案 ruleset_id={draft.ruleset_id!r} 与规则库 "
                    f"{ruleset.ruleset_id!r} 不一致",
                )
            )
        expected_hash = sha256_json(ruleset.model_dump(mode="json"))
        if draft.ruleset_hash and draft.ruleset_hash != expected_hash:
            result.errors.append(
                _issue(
                    "error",
                    "draft.ruleset_hash_mismatch",
                    "草案 ruleset_hash 与当前规则库不一致，请重新生成草案",
                )
            )

    explicit_scope = set(draft.scope_docs)
    for doc in explicit_scope:
        if doc not in documents:
            result.errors.append(
                _issue(
                    "error",
                    "scope.doc_unknown",
                    f"scope_docs 中的文档不在 context 中: {doc}",
                )
            )
    scope_docs = set(explicit_scope)
    seen_anchors: dict[tuple[str, str], int] = {}

    known_mq_producers = {
        (str(item.get("client") or ""), str(item.get("topic") or ""))
        for profile in context.get("profiles") or []
        for item in profile.get("mq_producers") or []
        if item.get("client") and item.get("topic")
    }
    for index, draft_case in enumerate(draft.cases):
        pseudo_path = f"cases[{index}]"
        key = draft_case.case_key
        endpoint_raw = draft_case.endpoint

        if not CASE_KEY_RE.match(key):
            result.errors.append(
                _issue(
                    "error",
                    "case.key_invalid",
                    f"case_key 必须匹配 {CASE_KEY_RE.pattern}: {key!r}",
                    case_key=key,
                    endpoint=endpoint_raw,
                    path=pseudo_path,
                )
            )
        if not CATEGORY_RE.match(draft_case.category):
            result.errors.append(
                _issue(
                    "error",
                    "case.category_invalid",
                    f"category 必须是小写安全标识: {draft_case.category!r}",
                    case_key=key,
                    endpoint=endpoint_raw,
                    path=pseudo_path,
                )
            )
        _check_mq_assertions(
            draft_case,
            known_producers=known_mq_producers,
            case_key=key,
            endpoint=endpoint_raw,
            path=pseudo_path,
            result=result,
        )
        _check_case_data(
            draft_case,
            case_key=key,
            endpoint=endpoint_raw,
            path=pseudo_path,
            result=result,
        )
        _check_mock_bindings(
            draft_case,
            case_key=key,
            endpoint=endpoint_raw,
            path=pseudo_path,
            result=result,
        )
        _check_level_refs(
            draft_case,
            case_key=key,
            endpoint=endpoint_raw,
            path=pseudo_path,
            result=result,
        )
        _check_assert_strength(
            draft_case,
            case_key=key,
            endpoint=endpoint_raw,
            path=pseudo_path,
            result=result,
        )
        _check_rule_refs(
            draft_case,
            ruleset=ruleset,
            require_rules=require_rules,
            case_key=key,
            endpoint=endpoint_raw,
            path=pseudo_path,
            result=result,
        )
        _check_preconditions(
            draft_case,
            data_catalog=data_catalog,
            case_key=key,
            endpoint=endpoint_raw,
            path=pseudo_path,
            result=result,
        )

        profile, exact = find_profile(context, endpoint_raw)
        if profile is None:
            result.errors.append(
                _issue(
                    "error",
                    "endpoint.unknown",
                    f"endpoint 不在 context 中: {endpoint_raw!r}",
                    case_key=key,
                    endpoint=endpoint_raw,
                    path=pseudo_path,
                )
            )
            _check_requirement_refs(
                draft_case,
                documents=documents,
                explicit_scope=explicit_scope,
                case_key=key,
                endpoint=endpoint_raw,
                path=pseudo_path,
                result=result,
                scope_docs=scope_docs,
            )
            continue

        canonical_endpoint = str(profile.get("endpoint") or "")
        if not exact:
            result.warnings.append(
                _issue(
                    "warning",
                    "endpoint.normalized",
                    f"endpoint 按 normalize_path 对齐为 {canonical_endpoint}",
                    case_key=key,
                    endpoint=canonical_endpoint,
                    path=pseudo_path,
                )
            )
        is_dubbo_profile = (
            str(profile.get("transport") or "http").lower() == "dubbo"
        )
        if is_dubbo_profile:
            if not profile.get("service") or not profile.get("method"):
                result.errors.append(
                    _issue(
                        "error",
                        "endpoint.invalid_context",
                        f"Dubbo profile 缺少 service/method: {canonical_endpoint!r}",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
                continue
        else:
            parsed = parse_endpoint(canonical_endpoint)
            if parsed is None or not profile.get("method") or not profile.get("path"):
                result.errors.append(
                    _issue(
                        "error",
                        "endpoint.invalid_context",
                        f"context profile 缺少 method/path: {canonical_endpoint!r}",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
                continue

        anchor = (canonical_endpoint, key)
        if anchor in seen_anchors:
            result.errors.append(
                _issue(
                    "error",
                    "case.duplicate_key",
                    f"(endpoint, case_key) 重复: {canonical_endpoint} / {key}",
                    case_key=key,
                    endpoint=canonical_endpoint,
                    path=pseudo_path,
                )
            )
        seen_anchors[anchor] = index

        canonical_draft = draft_case.model_copy(
            update={"endpoint": canonical_endpoint}
        )
        if (
            canonical_draft.transport is Transport.DUBBO
            and not canonical_draft.steps
        ):
            canonical_draft = _hydrate_dubbo_call(
                canonical_draft, profile=profile
            )
        _check_profile_alignment(
            canonical_draft,
            profile=profile,
            case_key=key,
            endpoint=canonical_endpoint,
            path=pseudo_path,
            result=result,
        )
        profile_deps = {
            str(dep.get("dep_id"))
            for dep in profile.get("downstream_deps") or []
            if dep.get("dep_id")
        }
        resolved_steps: list[tuple[CaseStep, dict[str, Any]]] = []
        if canonical_draft.steps:
            if len(canonical_draft.steps) < 2:
                result.errors.append(
                    _issue(
                        "error",
                        "case.steps_too_few",
                        "steps 至少需要 2 步；单请求请直接使用顶层请求字段",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
            if (
                canonical_draft.body is not None
                or canonical_draft.path_params
                or canonical_draft.query
                or canonical_draft.headers
                or canonical_draft.asserts
            ):
                result.errors.append(
                    _issue(
                        "error",
                        "case.steps_mixed_request",
                        "使用 steps 时顶层 body/path_params/query/headers/asserts 必须为空",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
            resolved_steps = _resolve_case_steps(
                canonical_draft,
                context,
                case_key=key,
                endpoint=canonical_endpoint,
                path=pseudo_path,
                result=result,
            )
            for _step, step_profile in resolved_steps:
                profile_deps |= {
                    str(dep.get("dep_id"))
                    for dep in step_profile.get("downstream_deps") or []
                    if dep.get("dep_id")
                }
            if resolved_steps and canonical_endpoint not in {
                step.endpoint for step, _ in resolved_steps
            }:
                result.errors.append(
                    _issue(
                        "error",
                        "case.steps_primary_missing",
                        f"顶层 endpoint {canonical_endpoint!r} 必须出现在 steps 中",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
            for index, (step, step_profile) in enumerate(resolved_steps):
                step_path = f"{pseudo_path}.steps[{index}]"
                if step.transport is not Transport.DUBBO:
                    _check_request(
                        step,
                        category=canonical_draft.category,
                        case_key=key,
                        endpoint=step.endpoint,
                        profile=step_profile,
                        path=step_path,
                        result=result,
                    )
                if not step.asserts:
                    result.errors.append(
                        _issue(
                            "error",
                            "assertion.missing",
                            f"步骤 {step.name!r} 至少需要一条断言",
                            case_key=key,
                            endpoint=step.endpoint,
                            path=step_path,
                        )
                    )
                required_kind = (
                    "rpc_status"
                    if step.transport is Transport.DUBBO
                    else "status_code"
                )
                if not any(
                    assertion.kind == required_kind for assertion in step.asserts
                ):
                    result.errors.append(
                        _issue(
                            "error",
                            (
                                "assertion.rpc_status_missing"
                                if required_kind == "rpc_status"
                                else "assertion.status_missing"
                            ),
                            (
                                f"步骤 {step.name!r} 至少需要一条 rpc_status 断言"
                                if required_kind == "rpc_status"
                                else f"步骤 {step.name!r} 至少需要一条 status_code 断言"
                            ),
                            case_key=key,
                            endpoint=step.endpoint,
                            path=step_path,
                        )
                    )
                for assertion in step.asserts:
                    _check_assert(
                        assertion,
                        case_key=key,
                        endpoint=step.endpoint,
                        profile=step_profile,
                        path=step_path,
                        result=result,
                        data_catalog=data_catalog,
                    )
                _check_grounding(
                    step.asserts,
                    canonical_draft.requirement_refs,
                    case_key=key,
                    endpoint=step.endpoint,
                    profile=step_profile,
                    path=step_path,
                    result=result,
                )
        _check_mock_semantics(
            canonical_draft,
            profile_deps=profile_deps,
            mock_semantic=mock_semantic,
            contracts=contracts,
            case_key=key,
            endpoint=canonical_endpoint,
            path=pseudo_path,
            result=result,
        )
        hydrated_draft = canonical_draft.model_copy(
            update={
                "asserts": _hydrate_schema_asserts(
                    canonical_draft.asserts, profile
                )
            }
        )
        hydrated_steps = [
            (
                step.model_copy(
                    update={
                        "asserts": _hydrate_schema_asserts(
                            step.asserts, step_profile
                        )
                    }
                ),
                step_profile,
            )
            for step, step_profile in resolved_steps
        ]
        case = to_test_case(
            hydrated_draft,
            method=str(profile["method"]),
            path=str(profile["path"]),
            doc_versions=result.doc_versions,
            generator=draft.generator,
            ruleset=ruleset,
            steps=[step for step, _ in hydrated_steps],
        )
        _check_requirement_refs(
            canonical_draft,
            documents=documents,
            explicit_scope=explicit_scope,
            case_key=key,
            endpoint=canonical_endpoint,
            path=pseudo_path,
            result=result,
            scope_docs=scope_docs,
        )
        if not canonical_draft.steps:
            for location, value, allow_response_expr in (
                ("body", case.body, False),
                ("query", case.query, False),
                ("path_params", case.path_params, False),
                *(
                    (
                        f"asserts[{i}].expect",
                        assertion.expect,
                        assertion.kind in {"body_field", "db_row"},
                    )
                    for i, assertion in enumerate(case.asserts)
                    if assertion.kind != "body_schema"
                ),
                *(
                    (f"asserts[{i}].where", assertion.where, True)
                    for i, assertion in enumerate(case.asserts)
                    if assertion.kind == "db_row" and assertion.where
                ),
            ):
                _check_step_refs(
                    value,
                    allowed_steps=set(),
                    allow_response_expr=allow_response_expr,
                    location=location,
                    case_key=key,
                    endpoint=canonical_endpoint,
                    path=pseudo_path,
                    result=result,
                )
            if has_dynamic(case.headers):
                result.errors.append(
                    _issue(
                        "error",
                        "step.header_ref_unsupported",
                        "Header 值不支持 $ref、$field 或表达式；请使用固定字符串",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
            if case.transport is not Transport.DUBBO:
                _check_request(
                    case,
                    category=canonical_draft.category,
                    case_key=key,
                    endpoint=canonical_endpoint,
                    profile=profile,
                    path=pseudo_path,
                    result=result,
                )
            if not case.asserts:
                result.errors.append(
                    _issue(
                        "error",
                        "assertion.missing",
                        "用例至少需要一条断言",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
            required_kind = (
                "rpc_status"
                if case.transport is Transport.DUBBO
                else "status_code"
            )
            if not any(
                assertion.kind == required_kind for assertion in case.asserts
            ):
                result.errors.append(
                    _issue(
                        "error",
                        (
                            "assertion.rpc_status_missing"
                            if required_kind == "rpc_status"
                            else "assertion.status_missing"
                        ),
                        (
                            "Dubbo 用例至少需要一条 rpc_status 断言"
                            if required_kind == "rpc_status"
                            else "用例至少需要一条 status_code 断言"
                        ),
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
            for assertion in case.asserts:
                _check_assert(
                    assertion,
                    case_key=key,
                    endpoint=canonical_endpoint,
                    profile=profile,
                    path=pseudo_path,
                    result=result,
                    data_catalog=data_catalog,
                )
            _check_grounding(
                case.asserts,
                case.requirement_refs,
                case_key=key,
                endpoint=canonical_endpoint,
                profile=profile,
                path=pseudo_path,
                result=result,
            )
        result.resolved.append(
            ResolvedDraftCase(
                draft_case=canonical_draft,
                case=case,
                fingerprint=case_fingerprint(case, key),
                profile=profile,
            )
        )

    for index, uncovered in enumerate(draft.uncovered_requirements):
        scope_docs.add(uncovered.doc)
        document = documents.get(uncovered.doc)
        if document is None:
            result.errors.append(
                _issue(
                    "error",
                    "uncovered.doc_unknown",
                    f"未覆盖需求文档不在 context 中: {uncovered.doc}",
                    path=f"uncovered_requirements[{index}]",
                )
            )
            continue
        sections = {
            str(section["section"]): section
            for section in document.get("sections") or []
        }
        section = sections.get(uncovered.section)
        if section is None:
            result.errors.append(
                _issue(
                    "error",
                    "uncovered.section_unknown",
                    f"未覆盖需求 section 不存在: {uncovered.doc}#{uncovered.section}",
                    path=f"uncovered_requirements[{index}]",
                )
            )
            continue
        if not section.get("include", True):
            result.warnings.append(
                _issue(
                    "warning",
                    "uncovered.metadata_section",
                    f"未覆盖声明指向元数据 section，不计入覆盖率: {uncovered.doc}#{uncovered.section}",
                    path=f"uncovered_requirements[{index}]",
                )
            )

    covered: set[tuple[str, str]] = set()
    for resolved in result.resolved:
        for ref in resolved.case.requirement_refs:
            if ref.doc and ref.section:
                covered.add((ref.doc, ref.section))
    declared_uncovered = {
        (item.doc, item.section) for item in draft.uncovered_requirements
    }
    for doc, section in context_included_sections(context):
        if (doc, section) in covered or (doc, section) in declared_uncovered:
            continue
        if doc not in scope_docs:
            continue
        result.unaccounted.append({"doc": doc, "section": section})
    if result.unaccounted:
        result.warnings.append(
            _issue(
                "warning",
                "requirement.unaccounted",
                f"{len(result.unaccounted)} 个需求 section 既未覆盖也未声明未覆盖",
            )
        )

    _check_rule_coverage(draft, ruleset=ruleset, result=result)
    _check_relation_coverage(draft, ruleset=ruleset, result=result)

    if check_doc_hashes:
        from .context import stale_documents

        stale = stale_documents(context, project)
        for item in stale:
            result.errors.append(
                _issue(
                    "error",
                    "context.stale",
                    f"需求文档已变化，请重新 cases prepare: {item['doc']}"
                    f"（{item['reason']}）",
                )
            )

    result.scope_docs = sorted(scope_docs)
    return result

__all__ = [
    "ResolvedDraftCase",
    "ValidationIssue",
    "ValidationResult",
    "validate_draft",
]
