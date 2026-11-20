"""校验：semantics。"""
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


def _check_mock_semantics(
    draft_case: DraftCase,
    *,
    profile_deps: set[str],
    mock_semantic: MockSemantic | None,
    contracts: dict[str, dict[str, Any]],
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    if mock_semantic is None or not draft_case.mock_bindings:
        return
    deps = mock_semantic.dep_index()
    known_profile_deps = profile_deps
    for dep_id, branch in draft_case.mock_bindings.items():
        if dep_id not in deps:
            result.errors.append(
                _issue(
                    "error",
                    "mock.dep_unknown",
                    f"mock_bindings 的 dep_id 不在 mock_semantics 中: {dep_id!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif known_profile_deps and dep_id not in known_profile_deps:
            result.errors.append(
                _issue(
                    "error",
                    "mock.dep_not_in_profile",
                    f"dep_id 不属于当前接口画像的下游依赖: {dep_id!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif branch not in {item.branch for item in deps[dep_id].branches}:
            result.errors.append(
                _issue(
                    "error",
                    "mock.branch_unknown",
                    f"{dep_id!r} 不存在语义分支 {branch!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if dep_id in deps:
            allowed = {item.branch for item in deps[dep_id].branches}
            for index, rule in enumerate(draft_case.mock_rules):
                if rule.dep_id == dep_id and rule.branch not in allowed:
                    result.errors.append(
                        _issue(
                            "error",
                            "mock.rule_branch_unknown",
                            f"mock_rules[{index}] 引用了未知分支 {rule.branch!r}",
                            case_key=case_key,
                            endpoint=endpoint,
                            path=path,
                        )
                    )
            for sequence_branch in draft_case.mock_sequences.get(dep_id, []):
                if sequence_branch not in allowed:
                    result.errors.append(
                        _issue(
                            "error",
                            "mock.sequence_branch_unknown",
                            f"mock_sequences[{dep_id!r}] 引用了未知分支 "
                            f"{sequence_branch!r}",
                            case_key=case_key,
                            endpoint=endpoint,
                            path=path,
                        )
                    )
        if branch != "success":
            continue
        spec = next(
            (item for item in deps[dep_id].branches if item.branch == branch), None
        )
        missing = _missing_contract_fields(
            spec.stub.body if spec is not None else None, contracts.get(dep_id)
        )
        if missing:
            contract = contracts[dep_id]
            label = contract.get("type") or "响应类型"
            result.warnings.append(
                _issue(
                    "warning",
                    "mock.body_contract_drift",
                    f"{dep_id!r} 的 success 分支缺少 {label} 的字段: "
                    f"{', '.join(missing)}；下游契约或 Mock 语义可能已过期",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    for dep_id, reply in draft_case.mock_replies.items():
        dep = deps.get(dep_id)
        if dep is not None and dep.transport not in (
            Transport.HTTP,
            Transport.DUBBO,
        ):
            result.errors.append(
                _issue(
                    "error",
                    "mock.reply_transport_unsupported",
                    f"mock_replies 目前只支持 HTTP/Dubbo 下游: {dep_id!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        branch_name = draft_case.mock_bindings.get(dep_id)
        spec = next(
            (
                item
                for item in (dep.branches if dep is not None else [])
                if item.branch == branch_name
            ),
            None,
        )
        body = _semantic_branch_body(spec) if spec is not None else None
        if isinstance(body, dict) and body and isinstance(reply, dict):
            unknown = sorted(set(reply) - set(body))
            if unknown:
                result.warnings.append(
                    _issue(
                        "warning",
                        "mock.reply_field_unknown",
                        f"{dep_id!r} 的 mock_replies 含分支响应体没有的字段: "
                        f"{', '.join(unknown)}；请确认不是造数越界",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )
