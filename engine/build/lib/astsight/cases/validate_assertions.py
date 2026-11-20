"""校验：assertions。"""
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


def _check_assert(
    assertion: Any,
    *,
    case_key: str,
    endpoint: str,
    profile: dict[str, Any],
    path: str,
    result: ValidationResult,
    data_catalog: DataCatalog | None = None,
) -> None:
    matrix = {
        "status_code": {"eq", "in", "between"},
        "body_field": {"exists", "eq", "ne", "in", "gte", "lte", "contains"},
        "header": {"exists", "eq", "contains"},
        "body_schema": {"matches"},
        "db_row": {
            "exists", "absent", "eq", "ne", "gte", "lte", "count_eq",
            "unchanged",
        },
        "rpc_status": {"eq", "ne", "in"},
        "result_present": {"exists", "absent"},
        "result_field": {"exists", "eq", "ne", "in", "gte", "lte", "contains"},
        "error_code": {"eq", "ne", "in", "contains"},
        "exception_type": {"eq", "ne", "contains"},
        "error_message": {"eq", "contains"},
    }
    is_dubbo = str(profile.get("transport") or "http").lower() == "dubbo"
    http_only = {"status_code", "body_field", "header", "body_schema"}
    dubbo_only = {
        "rpc_status",
        "result_present",
        "result_field",
        "error_code",
        "exception_type",
        "error_message",
    }
    if is_dubbo and assertion.kind in http_only:
        result.errors.append(
            _issue(
                "error",
                "assertion.transport_mismatch",
                f"Dubbo 用例不能使用 HTTP 断言类型: {assertion.kind}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    if not is_dubbo and assertion.kind in dubbo_only:
        result.errors.append(
            _issue(
                "error",
                "assertion.transport_mismatch",
                f"HTTP 用例不能使用 Dubbo 断言类型: {assertion.kind}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    if assertion.kind not in matrix:
        result.errors.append(
            _issue(
                "error",
                "assertion.unsupported_kind",
                f"不支持的断言类型: {assertion.kind}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    if assertion.op not in matrix[assertion.kind]:
        result.errors.append(
            _issue(
                "error",
                "assertion.unsupported_op",
                f"{assertion.kind} 不支持 op={assertion.op}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    wait_ms = int(getattr(assertion, "wait_ms", 0) or 0)
    if wait_ms and assertion.kind != "db_row":
        result.errors.append(
            _issue(
                "error",
                "assertion.wait_not_supported",
                "wait_ms 只用于 db_row 异步落库轮询",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    relation_ref = str(getattr(assertion, "relation_ref", "") or "")
    if relation_ref and assertion.kind not in {"body_field", "db_row"}:
        result.errors.append(
            _issue(
                "error",
                "relation.assertion_kind_invalid",
                "relation_ref 只能挂在 body_field 或 db_row 断言",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )

    if assertion.kind == "body_schema":
        _check_schema_assert(
            assertion,
            case_key=case_key,
            endpoint=endpoint,
            profile=profile,
            path=path,
            result=result,
        )
        return
    if assertion.kind == "db_row":
        _check_db_assert(
            assertion,
            data_catalog=data_catalog,
            case_key=case_key,
            endpoint=endpoint,
            path=path,
            result=result,
        )
        return
    if assertion.kind in dubbo_only:
        _check_dubbo_assert(
            assertion,
            case_key=case_key,
            endpoint=endpoint,
            path=path,
            result=result,
        )
        return

    if assertion.kind in {"body_field", "header"}:
        if not assertion.target:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.target_missing",
                    f"{assertion.kind} 断言缺少 target",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if assertion.kind == "body_field" and assertion.target and "." in assertion.target:
            result.warnings.append(
                _issue(
                    "warning",
                    "assertion.nested_body_field",
                    f"body_field 仅按顶层字段取值，嵌套路径可能不符合预期: {assertion.target}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if assertion.op != "exists" and assertion.expect is None:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.expect_missing",
                    f"{assertion.kind}/{assertion.op} 缺少 expect",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    else:
        if assertion.op == "between":
            expect = assertion.expect
            if (
                not isinstance(expect, (list, tuple))
                or len(expect) != 2
                or any(
                    not isinstance(item, int) or isinstance(item, bool)
                    for item in expect
                )
            ):
                result.errors.append(
                    _issue(
                        "error",
                        "assertion.between_invalid",
                        "status_code between 的 expect 必须是两个整数",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )
        elif assertion.op == "in":
            expect = assertion.expect
            if not isinstance(expect, (list, tuple)) or not expect:
                result.errors.append(
                    _issue(
                        "error",
                        "assertion.in_invalid",
                        "status_code in 的 expect 必须是非空整数列表",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )
            elif any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in expect
            ):
                result.errors.append(
                    _issue(
                        "error",
                        "assertion.in_invalid",
                        "status_code in 的 expect 必须只包含整数",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )
        elif not isinstance(assertion.expect, int) or isinstance(assertion.expect, bool):
            result.errors.append(
                _issue(
                    "error",
                    "assertion.status_invalid",
                    "status_code eq 的 expect 必须是整数",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )

    if assertion.kind == "body_field" and assertion.target:
        known = _response_fields(profile)
        if known and _top_level(assertion.target) not in known:
            severity = "error" if assertion.relation_ref else "warning"
            code = (
                "relation.response_field_unmapped"
                if assertion.relation_ref
                else "assertion.response_field_unknown"
            )
            message = (
                f"规则关系目标字段未在 OpenAPI 中声明: {assertion.target}"
                if assertion.relation_ref
                else f"响应字段未在 OpenAPI 中声明: {assertion.target}"
            )
            issue = _issue(
                severity,
                code,
                message,
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
            if severity == "error":
                result.errors.append(issue)
            else:
                result.warnings.append(issue)
        for field_path in field_paths(assertion.expect):
            if known and _top_level(field_path) not in known:
                result.warnings.append(
                    _issue(
                        "warning",
                        "assertion.response_field_unknown",
                        f"关系式响应字段未在 OpenAPI 中声明: {field_path}",
                        case_key=case_key,
                        endpoint=endpoint,
                        path=path,
                    )
                )


def _check_dubbo_assert(
    assertion: Any,
    *,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    kind = assertion.kind
    op = assertion.op
    expect = assertion.expect
    if kind == "rpc_status":
        if assertion.target:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.rpc_status_target",
                    "rpc_status 断言不接受 target",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        values = expect if isinstance(expect, list) else [expect]
        if not values or not all(isinstance(item, str) for item in values):
            result.errors.append(
                _issue(
                    "error",
                    "assertion.rpc_status_expect",
                    "rpc_status 的 expect 必须是字符串或字符串数组",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        unknown = [item for item in values if item not in _DUBBO_RPC_STATUSES]
        if unknown:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.rpc_status_unknown",
                    f"未知 rpc_status: {unknown}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        return
    if kind == "result_present":
        if assertion.target:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.result_present_target",
                    "result_present 断言不接受 target",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if expect is not None:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.result_present_expect",
                    "result_present 不需要 expect",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        return
    if kind == "result_field":
        if not assertion.target:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.target_missing",
                    "result_field 断言缺少 target",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    if op != "exists" and expect is None:
        result.errors.append(
            _issue(
                "error",
                "assertion.expect_missing",
                f"{kind}/{op} 缺少 expect",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )


def _check_schema_assert(
    assertion: Any,
    *,
    case_key: str,
    endpoint: str,
    profile: dict[str, Any],
    path: str,
    result: ValidationResult,
) -> None:
    status = str(assertion.target or "").strip()
    if not status.isdigit():
        result.errors.append(
            _issue(
                "error",
                "assertion.schema_status_invalid",
                "body_schema 的 target 必须是响应状态码",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    schemas = profile.get("response_schemas") or {}
    if status not in schemas:
        result.errors.append(
            _issue(
                "error",
                "assertion.schema_unknown",
                f"OpenAPI 未声明状态码 {status} 的 JSON Schema",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    if assertion.expect is not None and not isinstance(assertion.expect, dict):
        result.errors.append(
            _issue(
                "error",
                "assertion.schema_invalid",
                "body_schema 的 expect 必须是 JSON Schema 对象或留空由框架回填",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )


def _check_db_assert(
    assertion: Any,
    *,
    data_catalog: DataCatalog | None,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    target = str(assertion.target or "").strip()
    if not _DB_TARGET_RE.match(target):
        result.errors.append(
            _issue(
                "error",
                "assertion.db_target_invalid",
                f"db_row 的 target 必须是 table 或 table.column: {target!r}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    table, _, column = target.partition(".")
    op = assertion.op
    if op in {"eq", "ne", "gte", "lte"} and not column:
        result.errors.append(
            _issue(
                "error",
                "assertion.db_column_missing",
                f"db_row/{op} 需要 table.column 形式的 target",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if op in {"exists", "absent", "count_eq", "unchanged"} and column:
        result.errors.append(
            _issue(
                "error",
                "assertion.db_column_unexpected",
                f"db_row/{op} 只接受表名，不接受列: {target!r}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if assertion.ignore_columns and op != "unchanged":
        result.errors.append(
            _issue(
                "error",
                "assertion.db_ignore_unexpected",
                "ignore_columns 只用于 db_row/unchanged",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    if op == "unchanged":
        if not assertion.where:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_unchanged_where_missing",
                    "db_row/unchanged 必须用 where 指定要比较的行",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if assertion.expect is not None:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_expect_unexpected",
                    "db_row/unchanged 不需要 expect",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if assertion.wait_ms:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_unchanged_wait_unsupported",
                    "db_row/unchanged 暂不支持 wait_ms；异步一致性请用 eq/count_eq 轮询",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    elif op == "count_eq":
        if not isinstance(assertion.expect, int) or isinstance(assertion.expect, bool):
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_count_invalid",
                    "db_row/count_eq 的 expect 必须是非负整数",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif assertion.expect < 0:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_count_invalid",
                    "db_row/count_eq 的 expect 不能为负数",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    elif op in {"exists", "absent"}:
        if assertion.expect is not None:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_expect_unexpected",
                    f"db_row/{op} 不需要 expect",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    elif assertion.expect is None:
        result.errors.append(
            _issue(
                "error",
                "assertion.db_expect_missing",
                f"db_row/{op} 缺少 expect",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for name in assertion.ignore_columns or []:
        if not _IDENTIFIER_RE.match(str(name)):
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_ignore_invalid",
                    f"db_row ignore_columns 列名非法: {name!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    for name, value in (assertion.where or {}).items():
        if not _IDENTIFIER_RE.match(str(name)):
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_where_invalid",
                    f"db_row where 列名非法: {name!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        elif value is None:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_where_invalid",
                    f"db_row where.{name} 不能为 null；空值匹配请显式设计条件",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    if data_catalog is None:
        result.warnings.append(
            _issue(
                "warning",
                "assertion.db_catalog_missing",
                "声明了 db_row 断言但未提供 data_catalog，无法校验表/列是否存在",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    tables = data_catalog.table_index()
    table_spec = tables.get(table)
    if table_spec is None:
        result.errors.append(
            _issue(
                "error",
                "assertion.db_table_unknown",
                f"db_row 表不在 data_catalog 中: {table!r}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
        return
    columns = table_spec.column_index()
    if column and column not in columns:
        result.errors.append(
            _issue(
                "error",
                "assertion.db_column_unknown",
                f"db_row 列不在 data_catalog 中: {table}.{column}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for name in assertion.where or {}:
        if name not in columns:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_column_unknown",
                    f"db_row where 列不在 data_catalog 中: {table}.{name}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
    for name in assertion.ignore_columns or []:
        if name not in columns:
            result.errors.append(
                _issue(
                    "error",
                    "assertion.db_column_unknown",
                    f"db_row ignore_columns 列不在 data_catalog 中: "
                    f"{table}.{name}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )


def _hydrate_schema_asserts(
    assertions: list[Any], profile: dict[str, Any]
) -> list[Any]:
    """body_schema 未显式给 expect 时，用 OpenAPI schema 回填成可独立执行的断言。"""
    schemas = profile.get("response_schemas") or {}
    out: list[Any] = []
    for assertion in assertions:
        if (
            assertion.kind == "body_schema"
            and assertion.expect is None
            and str(assertion.target or "") in schemas
        ):
            assertion = assertion.model_copy(
                update={"expect": copy.deepcopy(schemas[str(assertion.target)])}
            )
        out.append(assertion)
    return out
