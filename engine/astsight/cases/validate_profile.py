"""校验：profile。"""
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
from .validate_dubbo import _check_dubbo_call


def _check_profile_alignment(
    draft_case: DraftCase,
    *,
    profile: dict[str, Any],
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    profile_transport = str(profile.get("transport") or Transport.HTTP.value).lower()
    if draft_case.transport not in {Transport.HTTP, Transport.DUBBO}:
        result.errors.append(
            _issue(
                "error",
                "transport.unsupported",
                f"只支持 http/dubbo transport，草案为 {draft_case.transport.value}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    elif profile_transport and profile_transport != draft_case.transport.value:
        result.errors.append(
            _issue(
                "error",
                "transport.mismatch",
                f"草案 transport={draft_case.transport.value} 与画像 "
                f"{profile_transport} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if draft_case.transport is Transport.DUBBO:
        _check_dubbo_call(
            draft_case,
            profile=profile,
            case_key=case_key,
            endpoint=endpoint,
            path=path,
            result=result,
        )
    profile_role = str(profile.get("coverage_role") or "primary").lower()
    if profile_role != draft_case.coverage_role.value:
        result.errors.append(
            _issue(
                "error",
                "coverage_role.mismatch",
                f"草案 coverage_role={draft_case.coverage_role.value} 与画像 "
                f"{profile_role} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )


def _check_preconditions(
    draft_case: DraftCase,
    *,
    data_catalog: DataCatalog | None,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    if not draft_case.preconditions:
        return
    for item in draft_case.preconditions:
        if item.kind is PreconditionKind.API_STATE:
            result.errors.append(
                _issue(
                    "error",
                    "precondition.api_state_unsupported",
                    "前置数据只支持插表造数（db_state）；"
                    "不通过调用业务接口构造测试数据",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    if data_catalog is None:
        result.warnings.append(
            _issue(
                "warning",
                "data_catalog.missing",
                "用例声明了前置数据状态，但未提供 data_catalog，执行阶段将标 blocked",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    known = data_catalog.state_index()
    for item in draft_case.preconditions:
        if item.state_key not in known:
            result.errors.append(
                _issue(
                    "error",
                    "precondition.state_unknown",
                    f"state_key 不在 data_catalog 中: {item.state_key!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
