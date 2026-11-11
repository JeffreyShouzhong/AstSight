"""草案校验：结构、契约、需求引用与资源上限。

校验只做确定性检查；不判断业务值是否合理（负例需要故意传非法值）。
错误阻止 proposal；警告进入审核文件/报告，不阻断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts import (
    CaseLevel,
    CaseStep,
    DataCatalog,
    MockSemantic,
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
from .draft import (
    CATEGORY_RE,
    CASE_KEY_RE,
    STEP_NAME_RE,
    CaseDraft,
    DraftCase,
    DraftCaseStep,
    case_fingerprint,
    to_test_case,
)
from ..persistence import sha256_json
from .requirements import normalize_text

_PATH_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_MAX_MOCK_BINDINGS = 64
_MAX_DEP_ID_LENGTH = 512
_MAX_BRANCH_LENGTH = 128
_ALLOWED_HEADERS = {
    "content-type",
    "accept",
    "authorization",
    "user-agent",
    "x-request-id",
    "x-correlation-id",
}
_GROUNDING_HINTS = (
    "400",
    "401",
    "403",
    "404",
    "409",
    "422",
    "429",
    "4xx",
    "5xx",
    "错误",
    "失败",
    "拒绝",
    "非法",
    "invalid",
    "reject",
    "error",
)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = Field(description="error / warning")
    code: str
    message: str
    case_key: str | None = None
    endpoint: str | None = None
    path: str | None = None


@dataclass
class ResolvedDraftCase:
    draft_case: DraftCase
    case: TestCase
    fingerprint: str
    profile: dict[str, Any]

    @property
    def case_key(self) -> str:
        return self.draft_case.case_key

    @property
    def endpoint(self) -> str:
        return self.case.endpoint


@dataclass
class ValidationResult:
    context_id: str
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    resolved: list[ResolvedDraftCase] = field(default_factory=list)
    scope_docs: list[str] = field(default_factory=list)
    unaccounted: list[dict[str, str]] = field(default_factory=list)
    doc_versions: dict[str, str] = field(default_factory=dict)
    requirements: list[RequirementDoc] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "ok": not self.errors,
            "errors": [item.model_dump() for item in self.errors],
            "warnings": [item.model_dump() for item in self.warnings],
            "scope_docs": self.scope_docs,
            "unaccounted_requirements": self.unaccounted,
            "doc_versions": self.doc_versions,
            "resolved_cases": [
                {
                    "case_key": resolved.case_key,
                    "endpoint": resolved.endpoint,
                    "category": resolved.case.category,
                    "fingerprint": resolved.fingerprint,
                }
                for resolved in self.resolved
            ],
        }


def _issue(
    severity: str,
    code: str,
    message: str,
    *,
    case_key: str | None = None,
    endpoint: str | None = None,
    path: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,
        code=code,
        message=message,
        case_key=case_key,
        endpoint=endpoint,
        path=path,
    )


def _top_level(key: str) -> str:
    return key.split(".", 1)[0] if key else ""


def _schema_properties(profile: dict[str, Any]) -> dict[str, Any]:
    body = profile.get("request_body") or {}
    return body.get("properties") or {}


def _body_allows_additional(profile: dict[str, Any]) -> bool:
    body = profile.get("request_body") or {}
    return bool(body.get("additionalProperties"))


def _query_names(profile: dict[str, Any]) -> set[str]:
    return {
        str(param.get("name"))
        for param in profile.get("params") or []
        if param.get("in") == "query"
    }


def _header_names(profile: dict[str, Any]) -> set[str]:
    return {
        str(param.get("name")).lower()
        for param in profile.get("params") or []
        if param.get("in") == "header"
    }


def _required_query(profile: dict[str, Any]) -> set[str]:
    return {
        str(param.get("name"))
        for param in profile.get("params") or []
        if param.get("in") == "query" and param.get("required")
    }


def _required_headers(profile: dict[str, Any]) -> set[str]:
    return {
        str(param.get("name")).lower()
        for param in profile.get("params") or []
        if param.get("in") == "header" and param.get("required")
    }


def _path_names(profile: dict[str, Any]) -> set[str]:
    path = str(profile.get("path") or "")
    return set(_PATH_PLACEHOLDER_RE.findall(path))


def _response_fields(profile: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for fields in (profile.get("response_fields") or {}).values():
        for name in fields or []:
            out.add(str(name))
    return out


def _expected_status_codes(assertion: Any) -> list[int]:
    expect = assertion.expect
    if assertion.op == "in" and isinstance(expect, (list, tuple)):
        return [item for item in expect if isinstance(item, int) and not isinstance(item, bool)]
    if assertion.op == "between" and isinstance(expect, (list, tuple)) and len(expect) == 2:
        return [
            item
            for item in expect
            if isinstance(item, int) and not isinstance(item, bool)
        ]
    if isinstance(expect, int) and not isinstance(expect, bool):
        return [expect]
    return []


def _iter_ref_nodes(value: Any, location: str):
    """深度遍历请求值/期望值中的 ``{"$ref": "step.field"}``。"""
    if isinstance(value, dict):
        if "$ref" in value:
            yield value, location
            return
        for key, item in value.items():
            yield from _iter_ref_nodes(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_ref_nodes(item, f"{location}[{index}]")


def _has_ref(value: Any) -> bool:
    return any(True for _ in _iter_ref_nodes(value, ""))


def _is_step_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"$ref"}
        and isinstance(value.get("$ref"), str)
    )


def _check_step_refs(
    value: Any,
    *,
    allowed_steps: set[str],
    location: str,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    for node, where in _iter_ref_nodes(value, location):
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

        allowed = set(seen_names) - {name}
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
            if _has_ref(header_value):
                result.errors.append(
                    _issue(
                        "error",
                        "step.header_ref_unsupported",
                        f"Header {header_name!r} 不支持 $ref；请使用固定字符串",
                        case_key=case_key,
                        endpoint=canonical,
                        path=step_label,
                    )
                )
                continue
            safe_headers[str(header_name)] = str(header_value)
        for assertion in raw.asserts:
            _check_step_refs(
                assertion.expect,
                allowed_steps=allowed,
                location=f"steps[{index}].asserts.expect",
                case_key=case_key,
                endpoint=canonical,
                path=step_label,
                result=result,
            )

        resolved.append(
            (
                CaseStep(
                    name=name,
                    endpoint=canonical,
                    method=str(profile["method"]).upper(),
                    path=str(profile["path"]),
                    path_params=dict(raw.path_params),
                    query=dict(raw.query),
                    headers=safe_headers,
                    body=raw.body,
                    asserts=[item.model_copy() for item in raw.asserts],
                ),
                profile,
            )
        )
    return resolved


def _check_assert(
    assertion: Any,
    *,
    case_key: str,
    endpoint: str,
    profile: dict[str, Any],
    path: str,
    result: ValidationResult,
) -> None:
    matrix = {
        "status_code": {"eq", "in", "between"},
        "body_field": {"exists", "eq", "ne", "in", "gte", "lte", "contains"},
        "header": {"exists", "eq"},
    }
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
            result.warnings.append(
                _issue(
                    "warning",
                    "assertion.response_field_unknown",
                    f"响应字段未在 OpenAPI 中声明: {assertion.target}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )


def _grounded_in_quote(codes: list[int], quote_text: str) -> bool:
    if not codes:
        return True
    lowered = quote_text.lower()
    if any(str(code) in lowered for code in codes):
        return True
    return any(hint in lowered for hint in _GROUNDING_HINTS)


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


def _check_mock_bindings(
    draft_case: DraftCase,
    *,
    case_key: str,
    endpoint: str,
    path: str,
    result: ValidationResult,
) -> None:
    """Mock 绑定进入执行会话，必须是可定位、非空的确定性字符串。"""
    if len(draft_case.mock_bindings) > _MAX_MOCK_BINDINGS:
        result.errors.append(
            _issue(
                "error",
                "mock.too_many_bindings",
                f"mock_bindings 数量 {len(draft_case.mock_bindings)} 超过上限 "
                f"{_MAX_MOCK_BINDINGS}",
                case_key=case_key,
                endpoint=endpoint,
                path=path,
            )
        )
    for dep_id, branch in draft_case.mock_bindings.items():
        if not dep_id.strip() or len(dep_id) > _MAX_DEP_ID_LENGTH:
            result.errors.append(
                _issue(
                    "error",
                    "mock.dep_id_invalid",
                    f"mock_bindings 的 dep_id 为空或过长: {dep_id[:80]!r}",
                    case_key=case_key,
                    endpoint=endpoint,
                    path=path,
                )
            )
        if not branch.strip() or len(branch) > _MAX_BRANCH_LENGTH:
            result.errors.append(
                _issue(
                    "error",
                    "mock.branch_invalid",
                    f"{dep_id[:80]!r} 的 branch 为空或过长: {branch[:80]!r}",
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
    if draft_case.transport is not Transport.HTTP:
        result.errors.append(
            _issue(
                "error",
                "transport.unsupported",
                f"v1 只支持 http transport，草案为 {draft_case.transport.value}",
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


def _check_mock_semantics(
    draft_case: DraftCase,
    *,
    profile_deps: set[str],
    mock_semantic: MockSemantic | None,
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
                if not any(
                    assertion.kind == "status_code" for assertion in step.asserts
                ):
                    result.errors.append(
                        _issue(
                            "error",
                            "assertion.status_missing",
                            f"步骤 {step.name!r} 至少需要一条 status_code 断言",
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
            case_key=key,
            endpoint=canonical_endpoint,
            path=pseudo_path,
            result=result,
        )
        case = to_test_case(
            canonical_draft,
            method=str(profile["method"]),
            path=str(profile["path"]),
            doc_versions=result.doc_versions,
            generator=draft.generator,
            ruleset=ruleset,
            steps=[step for step, _ in resolved_steps],
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
            for location, value in (
                ("body", case.body),
                ("query", case.query),
                ("path_params", case.path_params),
                *(
                    (f"asserts[{i}].expect", assertion.expect)
                    for i, assertion in enumerate(case.asserts)
                ),
            ):
                _check_step_refs(
                    value,
                    allowed_steps=set(),
                    location=location,
                    case_key=key,
                    endpoint=canonical_endpoint,
                    path=pseudo_path,
                    result=result,
                )
            if _has_ref(case.headers):
                result.errors.append(
                    _issue(
                        "error",
                        "step.header_ref_unsupported",
                        "Header 值不支持 $ref；请使用固定字符串",
                        case_key=key,
                        endpoint=canonical_endpoint,
                        path=pseudo_path,
                    )
                )
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
            if not any(
                assertion.kind == "status_code" for assertion in case.asserts
            ):
                result.errors.append(
                    _issue(
                        "error",
                        "assertion.status_missing",
                        "用例至少需要一条 status_code 断言",
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
