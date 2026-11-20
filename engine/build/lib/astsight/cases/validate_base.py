"""校验基础：模型、常量与共享 helpers。"""
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


_PATH_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


_DB_TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")


_MAX_MOCK_BINDINGS = 64


_MAX_DEP_ID_LENGTH = 512


_MAX_BRANCH_LENGTH = 128


_MAX_DATA_KEYS = 50


_MAX_DATA_REFS = 200


_DATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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
        if is_step_ref(value):
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


_DUBBO_RPC_STATUSES = {
    "OK",
    "TIMEOUT",
    "SERIALIZATION",
    "NETWORK",
    "FORBIDDEN",
    "NO_PROVIDER",
    "RPC_EXCEPTION",
    "BAD_REQUEST",
}


def _grounded_in_quote(codes: list[int], quote_text: str) -> bool:
    if not codes:
        return True
    lowered = quote_text.lower()
    if any(str(code) in lowered for code in codes):
        return True
    return any(hint in lowered for hint in _GROUNDING_HINTS)


def _dep_contracts(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按 dep_id 收集 A 侧响应契约；类型不唯一时不参与校验。"""
    contracts: dict[str, dict[str, Any]] = {}
    for profile in context.get("profiles") or []:
        for dep in profile.get("downstream_deps") or []:
            if not isinstance(dep, dict):
                continue
            dep_id = str(dep.get("dep_id") or "")
            contract = dep.get("response_contract")
            if not dep_id or not isinstance(contract, dict):
                continue
            types = contract.get("types") or []
            if len(types) != 1 or not isinstance(types[0], dict):
                continue
            fields = {
                str(field.get("name"))
                for field in types[0].get("fields") or []
                if isinstance(field, dict) and field.get("name")
            }
            if fields:
                contracts.setdefault(
                    dep_id,
                    {"type": str(types[0].get("name") or ""), "fields": fields},
                )
    return contracts


def _semantic_branch_body(branch: Any) -> Any:
    if getattr(branch, "reply", None) is not None:
        return branch.reply.body
    return branch.stub.body


def _stub_object(body: Any) -> dict[str, Any] | None:
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, list):
            return content[0] if content and isinstance(content[0], dict) else None
        return body
    if isinstance(body, list):
        return body[0] if body and isinstance(body[0], dict) else None
    return None


def _missing_contract_fields(
    body: Any, contract: dict[str, Any] | None
) -> list[str]:
    if not contract:
        return []
    stub = _stub_object(body)
    if stub is None:
        return []
    return sorted(name for name in contract["fields"] if name not in stub)


__all__ = [
    "ResolvedDraftCase",
    "ValidationIssue",
    "ValidationResult",
    "validate_draft",
]


__all__ = [
    '_PATH_PLACEHOLDER_RE',
    '_IDENTIFIER_RE',
    '_DB_TARGET_RE',
    '_MAX_MOCK_BINDINGS',
    '_MAX_DEP_ID_LENGTH',
    '_MAX_BRANCH_LENGTH',
    '_MAX_DATA_KEYS',
    '_MAX_DATA_REFS',
    '_DATA_KEY_RE',
    '_ALLOWED_HEADERS',
    '_GROUNDING_HINTS',
    'ValidationIssue',
    'ResolvedDraftCase',
    'ValidationResult',
    '_issue',
    '_top_level',
    '_schema_properties',
    '_body_allows_additional',
    '_query_names',
    '_header_names',
    '_required_query',
    '_required_headers',
    '_path_names',
    '_response_fields',
    '_expected_status_codes',
    '_iter_ref_nodes',
    '_has_ref',
    '_is_step_ref',
    '_DUBBO_RPC_STATUSES',
    '_grounded_in_quote',
    '_dep_contracts',
    '_semantic_branch_body',
    '_stub_object',
    '_missing_contract_fields',
    '__all__',
]
