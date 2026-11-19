"""校验：dubbo。"""
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


def _hydrate_dubbo(
    dubbo: DubboCall | None,
    *,
    profile: dict[str, Any],
) -> DubboCall:
    """接口/方法/参数类型以画像为准；草案只保留取值与附件。"""
    call = dubbo.model_copy(deep=True) if dubbo else DubboCall()
    updates: dict[str, Any] = {}
    if not call.interface:
        updates["interface"] = str(profile.get("service") or "")
    if not call.method:
        updates["method"] = str(profile.get("method") or "")
    if not call.parameter_types:
        updates["parameter_types"] = list(profile.get("parameter_types") or [])
    if call.group is None:
        updates["group"] = profile.get("group") or None
    if call.version is None:
        updates["version"] = profile.get("version") or None
    if not call.url:
        updates["url"] = profile.get("direct_url")
    if call.target_port is None:
        updates["target_port"] = profile.get("protocol_port")
    if call.serialization is None:
        updates["serialization"] = profile.get("serialization")
    if not call.descriptor_source:
        updates["descriptor_source"] = profile.get("descriptor_source")
    if not call.grpc_path:
        updates["grpc_path"] = profile.get("grpc_path")
    if call.compatibility is None:
        updates["compatibility"] = profile.get("compatibility")
    if updates:
        call = call.model_copy(update=updates)
    return call


def _hydrate_dubbo_call(
    draft_case: DraftCase,
    *,
    profile: dict[str, Any],
) -> DraftCase:
    if draft_case.steps and draft_case.dubbo is None:
        return draft_case
    return draft_case.model_copy(
        update={"dubbo": _hydrate_dubbo(draft_case.dubbo, profile=profile)}
    )


def _check_dubbo_call_fields(
    call: DubboCall | None,
    *,
    profile: dict[str, Any],
    path_params: dict[str, Any],
    query: dict[str, Any],
    headers: dict[str, Any],
    body: Any,
    require_call: bool,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    if call is None:
        if require_call:
            result.errors.append(
                _issue(
                    "error",
                    "dubbo.call_missing",
                    "transport=dubbo 的用例必须提供 dubbo 调用参数",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        return
    expected_service = str(profile.get("service") or "")
    expected_method = str(profile.get("method") or "")
    expected_types = list(profile.get("parameter_types") or [])
    if expected_service and call.interface != expected_service:
        result.errors.append(
            _issue(
                "error",
                "dubbo.interface_mismatch",
                f"dubbo.interface={call.interface!r} 与画像 {expected_service!r} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if expected_method and call.method != expected_method:
        result.errors.append(
            _issue(
                "error",
                "dubbo.method_mismatch",
                f"dubbo.method={call.method!r} 与画像 {expected_method!r} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if expected_types and call.parameter_types != expected_types:
        result.errors.append(
            _issue(
                "error",
                "dubbo.parameter_types_mismatch",
                f"dubbo.parameter_types={call.parameter_types!r} 与画像 "
                f"{expected_types!r} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    expected_group = str(profile.get("group") or "")
    expected_version = str(profile.get("version") or "")
    if (call.group or "") != expected_group:
        result.errors.append(
            _issue(
                "error",
                "dubbo.group_mismatch",
                f"dubbo.group={call.group!r} 与画像 {expected_group!r} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if (call.version or "") != expected_version:
        result.errors.append(
            _issue(
                "error",
                "dubbo.version_mismatch",
                f"dubbo.version={call.version!r} 与画像 {expected_version!r} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if len(call.arguments) != len(call.parameter_types):
        result.errors.append(
            _issue(
                "error",
                "dubbo.arguments_count",
                f"arguments 数量 {len(call.arguments)} 与参数类型数量 "
                f"{len(call.parameter_types)} 不一致",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if call.serialization and call.serialization.lower() not in {
        "hessian2",
        "protobuf",
        "proto",
    }:
        result.errors.append(
            _issue(
                "error",
                "dubbo.serialization_unsupported",
                f"不支持的 Dubbo serialization: {call.serialization}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if call.compatibility and call.compatibility not in {
        "native",
        "grpc_compatible",
    }:
        result.errors.append(
            _issue(
                "error",
                "dubbo.compatibility_unsupported",
                f"不支持的 Triple compatibility: {call.compatibility}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if call.serialization and call.serialization.lower() in {
        "protobuf",
        "proto",
    } and not call.descriptor_source:
        result.errors.append(
            _issue(
                "error",
                "dubbo.descriptor_missing",
                "Triple/gRPC-compatible 调用必须提供 descriptor_source",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if call.grpc_path and (
        not call.grpc_path.startswith("/")
        or call.grpc_path.count("/") != 2
    ):
        result.errors.append(
            _issue(
                "error",
                "dubbo.grpc_path_invalid",
                f"grpc_path 必须形如 /package.Service/Method: {call.grpc_path}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if path_params or query or body is not None or headers:
        result.errors.append(
            _issue(
                "error",
                "dubbo.http_fields_present",
                "transport=dubbo 的用例不能带 path_params/query/headers/body",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )


def _check_dubbo_call(
    draft_case: DraftCase,
    *,
    profile: dict[str, Any],
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    _check_dubbo_call_fields(
        draft_case.dubbo,
        profile=profile,
        path_params=draft_case.path_params,
        query=draft_case.query,
        headers=draft_case.headers,
        body=draft_case.body,
        require_call=not bool(draft_case.steps),
        case_key=case_key,
        endpoint=endpoint,
        path=path,
        result=result,
    )
