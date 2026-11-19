"""校验：steps。"""
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
from .validate_dubbo import _check_dubbo_call_fields, _hydrate_dubbo


def _check_step_refs(
    value: Any,
    *,
    allowed_steps: set[str],
    allow_response_expr: bool = False,
    location: str,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    for code, message in validate_dynamic(
        value, allow_response_expr=allow_response_expr
    ):
        result.errors.append(
            _issue(
                "error",
                code,
                message,
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for node, where in iter_dynamic_nodes(value, location=location):
        if not is_step_ref(node):
            continue
        if set(node) != {"$ref"}:
            result.errors.append(
                _issue(
                    "error",
                    "step.ref_invalid",
                    f"{where} 的 $ref 节点不能包含其他字段",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
            continue
        ref = node.get("$ref")
        if not isinstance(ref, str) or "." not in ref or len(ref) > 256:
            result.errors.append(
                _issue(
                    "error",
                    "step.ref_invalid",
                    f"{where} 的 $ref 必须是 '<step>.<field>' 且长度不超过 256",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
            continue
        step_name, _, field = ref.partition(".")
        if not STEP_NAME_RE.match(step_name) or not field:
            result.errors.append(
                _issue(
                    "error",
                    "step.ref_invalid",
                    f"{where} 的 $ref 步骤名或字段非法: {ref!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif step_name not in allowed_steps:
            result.errors.append(
                _issue(
                    "error",
                    "step.ref_unknown",
                    f"{where} 只能引用更早的步骤，未知引用: {ref!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )


def _resolve_case_steps(
    draft_case: DraftCase,
    context: dict[str, Any],
    *,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> list[tuple[CaseStep, dict[str, Any]]]:
    """解析多请求步骤：endpoint 对齐画像、名称唯一、引用只能向后。"""
    resolved: list[tuple[CaseStep, dict[str, Any]]] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(draft_case.steps):
        step_label = f"{path}.steps[{index}]"
        name = raw.name
        if not STEP_NAME_RE.match(name):
            result.errors.append(
                _issue(
                    "error",
                    "step.name_invalid",
                    f"步骤名必须匹配 {STEP_NAME_RE.pattern}: {name!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=step_label,
                )
            )
        if name in seen_names:
            result.errors.append(
                _issue(
                    "error",
                    "step.name_duplicate",
                    f"步骤名重复: {name!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=step_label,
                )
            )
        seen_names.add(name)

        profile, exact = find_profile(context, raw.endpoint)
        if profile is None:
            result.errors.append(
                _issue(
                    "error",
                    "step.endpoint_unknown",
                    f"步骤 endpoint 不在 context 中: {raw.endpoint!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=step_label,
                )
            )
            continue
        canonical = str(profile.get("endpoint") or "")
        if not exact:
            result.warnings.append(
                _issue(
                    "warning",
                    "step.endpoint_normalized",
                    f"步骤 endpoint 按 normalize_path 对齐为 {canonical}",
                    case_key=case_key,
                    endpoint=canonical,
                    path=step_label,
                )
            )
        is_dubbo = str(profile.get("transport") or "http").lower() == "dubbo"
        if is_dubbo:
            if not profile.get("service") or not profile.get("method"):
                result.errors.append(
                    _issue(
                        "error",
                        "step.endpoint_invalid_context",
                        f"step Dubbo profile 缺少 service/method: {canonical!r}",
                        case_key=case_key,
                        endpoint=canonical,
                        path=step_label,
                    )
                )
                continue
        else:
            parsed = parse_endpoint(canonical)
            if parsed is None or not profile.get("method") or not profile.get("path"):
                result.errors.append(
                    _issue(
                        "error",
                        "step.endpoint_invalid_context",
                        f"step profile 缺少 method/path: {canonical!r}",
                        case_key=case_key,
                        endpoint=canonical,
                        path=step_label,
                    )
                )
                continue
            if raw.dubbo is not None:
                result.errors.append(
                    _issue(
                        "error",
                        "step.dubbo_on_http",
                        "HTTP 步骤不能带 dubbo 调用参数",
                        case_key=case_key,
                        endpoint=canonical,
                        path=step_label,
                    )
                )

        allowed = set(seen_names) - {name}
        step_dubbo = (
            _hydrate_dubbo(raw.dubbo, profile=profile) if is_dubbo else None
        )
        if is_dubbo:
            _check_dubbo_call_fields(
                step_dubbo,
                profile=profile,
                path_params=raw.path_params,
                query=raw.query,
                headers=raw.headers,
                body=raw.body,
                require_call=True,
                case_key=case_key,
                endpoint=canonical,
                path=step_label,
                result=result,
            )
            if step_dubbo is not None:
                _check_step_refs(
                    step_dubbo.arguments,
                    allowed_steps=allowed,
                    location=f"steps[{index}].dubbo.arguments",
                    case_key=case_key,
                    endpoint=canonical,
                    path=step_label,
                    result=result,
                )
        _check_step_refs(
            raw.body,
            allowed_steps=allowed,
            location=f"steps[{index}].body",
            case_key=case_key,
            endpoint=canonical,
            path=step_label,
            result=result,
        )
        _check_step_refs(
            raw.query,
            allowed_steps=allowed,
            location=f"steps[{index}].query",
            case_key=case_key,
            endpoint=canonical,
            path=step_label,
            result=result,
        )
        _check_step_refs(
            raw.path_params,
            allowed_steps=allowed,
            location=f"steps[{index}].path_params",
            case_key=case_key,
            endpoint=canonical,
            path=step_label,
            result=result,
        )
        safe_headers: dict[str, str] = {}
        for header_name, header_value in raw.headers.items():
            if has_dynamic(header_value):
                result.errors.append(
                    _issue(
                        "error",
                        "step.header_ref_unsupported",
                        f"Header {header_name!r} 不支持动态表达式；请使用固定字符串",
                        case_key=case_key,
                        endpoint=canonical,
                        path=step_label,
                    )
                )
                continue
            safe_headers[str(header_name)] = str(header_value)
        for assertion in raw.asserts:
            if assertion.kind != "body_schema":
                _check_step_refs(
                    assertion.expect,
                    allowed_steps=allowed,
                    allow_response_expr=assertion.kind
                    in {"body_field", "db_row"}
                    or (is_dubbo and assertion.kind == "result_field"),
                    location=f"steps[{index}].asserts.expect",
                    case_key=case_key,
                    endpoint=canonical,
                    path=step_label,
                    result=result,
                )
            if assertion.kind == "db_row" and assertion.where:
                _check_step_refs(
                    assertion.where,
                    allowed_steps=allowed,
                    allow_response_expr=True,
                    location=f"steps[{index}].asserts.where",
                    case_key=case_key,
                    endpoint=canonical,
                    path=step_label,
                    result=result,
                )

        resolved.append(
            (
                CaseStep(
                    name=name,
                    transport=(
                        Transport.DUBBO if is_dubbo else Transport.HTTP
                    ),
                    endpoint=canonical,
                    method=(
                        str(profile["method"])
                        if is_dubbo
                        else str(profile["method"]).upper()
                    ),
                    path="" if is_dubbo else str(profile["path"]),
                    path_params=dict(raw.path_params),
                    query=dict(raw.query),
                    headers=safe_headers,
                    body=raw.body,
                    dubbo=step_dubbo,
                    asserts=[item.model_copy() for item in raw.asserts],
                ),
                profile,
            )
        )
    return resolved


def _check_request(
    case: TestCase,
    *,
    category: str,
    case_key: str,
    endpoint: str,
    profile: dict[str, Any],
    path: str,
    result: ValidationResult,
) -> None:
    path_names = _path_names(profile)
    supplied_path = set(case.path_params)
    missing_path = sorted(path_names - supplied_path)
    extra_path = sorted(supplied_path - path_names)
    if missing_path:
        result.errors.append(
            _issue(
                "error",
                "request.path_param_missing",
                f"缺少路径参数: {missing_path}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if extra_path:
        result.errors.append(
            _issue(
                "error",
                "request.path_param_unknown",
                f"多余的路径参数: {extra_path}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for name, value in case.path_params.items():
        if _is_step_ref(value):
            # 多请求用例允许路径参数引用更早步骤的响应；引用本身在执行期解析。
            continue
        if isinstance(value, (dict, list, tuple)) or value is None:
            result.errors.append(
                _issue(
                    "error",
                    "request.path_param_not_scalar",
                    f"路径参数 {name} 必须是标量",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )

    # 无契约时 params 为空，不能据此断言“参数未声明”——那是把缺失当成了否定。
    contract_known = not bool(profile.get("contract_incomplete"))
    query_names = _query_names(profile)
    unknown_query = sorted(set(case.query) - query_names) if contract_known else []
    if unknown_query:
        result.errors.append(
            _issue(
                "error",
                "request.query_unknown",
                f"查询参数未在契约中声明: {unknown_query}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    header_names = _header_names(profile)
    unknown_headers = (
        sorted(
            name
            for name in case.headers
            if name.lower() not in header_names
            and name.lower() not in _ALLOWED_HEADERS
        )
        if contract_known
        else []
    )
    if unknown_headers:
        result.errors.append(
            _issue(
                "error",
                "request.header_unknown",
                f"Header 未在契约中声明: {unknown_headers}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )

    properties = _schema_properties(profile)
    if case.body is not None and not isinstance(case.body, dict):
        result.warnings.append(
            _issue(
                "warning",
                "request.body_not_object",
                "请求体不是 JSON 对象；无法按契约检查顶层字段",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    elif isinstance(case.body, dict) and properties and not _body_allows_additional(profile):
        unknown_body = sorted(set(case.body) - set(properties))
        if unknown_body:
            result.errors.append(
                _issue(
                    "error",
                    "request.body_field_unknown",
                    f"请求体字段未在契约中声明: {unknown_body}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )

    if category in {"positive", "business"}:
        missing_required_body = [
            name for name in profile.get("body_required") or []
            if not isinstance(case.body, dict) or name not in case.body
        ]
        missing_required_query = sorted(_required_query(profile) - set(case.query))
        missing_required_headers = sorted(
            _required_headers(profile) - {name.lower() for name in case.headers}
        )
        for message in (
            f"正向用例缺少必填请求体字段: {missing_required_body}"
            if missing_required_body else "",
            f"正向用例缺少必填查询参数: {missing_required_query}"
            if missing_required_query else "",
            f"正向用例缺少必填 Header: {missing_required_headers}"
            if missing_required_headers else "",
        ):
            if message:
                result.warnings.append(
                    _issue(
                        "warning",
                        "request.required_missing",
                        message,
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )
