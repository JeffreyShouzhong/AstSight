"""A5/A6 失败现场打包：确定性归因之后，把疑难失败整理成 LLM 可消费的输入。

本模块只做选择与裁剪，不调用模型、不改草案；真正的诊断 + 修复合并节点在
`repair.py`。确定性归因保持原样，LLM 结论单独存放，修复前仍需过确定性护栏。
"""
from __future__ import annotations

import re
from typing import Any

from contracts import MockSemantic

MAX_FAILURES = 20
MAX_MESSAGE_CHARS = 8_000
_SECRET_HEADERS = ("authorization", "cookie", "token", "secret", "api-key", "x-api-key")
_5XX_RE = re.compile(r"\b5\d{2}\b")
ACTION_BY_ROOT = {
    "code_defect": "user",
    "case_error": "user",
    "change_induced": "user",
    "data_error": "framework",
    "mock_error": "framework",
    "adapter_error": "framework",
    "env_error": "framework",
}
REPAIRABLE_ROOTS = {"case_error", "mock_error"}


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if any(token in str(key).lower() for token in _SECRET_HEADERS):
            safe[str(key)] = "***"
        else:
            safe[str(key)] = str(value)[:500]
    return safe


def case_payload(case: Any, *, case_key: str | None = None) -> dict[str, Any]:
    refs = [
        {
            "doc": ref.doc,
            "section": ref.section,
            "quote": (ref.quote or "")[:500],
        }
        for ref in (getattr(case, "requirement_refs", None) or [])
    ]
    return {
        "id": getattr(case, "id", ""),
        "endpoint": getattr(case, "endpoint", ""),
        "case_key": case_key or getattr(case, "id", ""),
        "category": getattr(case, "category", ""),
        "title": getattr(case, "title", ""),
        "method": getattr(case, "method", ""),
        "path": getattr(case, "path", ""),
        "path_params": getattr(case, "path_params", {}) or {},
        "query": getattr(case, "query", {}) or {},
        "headers": _safe_headers(getattr(case, "headers", {}) or {}),
        "body": getattr(case, "body", None),
        "asserts": [
            assertion.model_dump(mode="json")
            for assertion in (getattr(case, "asserts", None) or [])
        ],
        "steps": [
            step.model_dump(mode="json")
            for step in (getattr(case, "steps", None) or [])
        ],
        "assert_strength": str(getattr(case, "assert_strength", "")),
        "requirement_refs": refs,
        "mock_bindings": dict(getattr(case, "mock_bindings", {}) or {}),
        "source": getattr(case, "source", ""),
    }


def mock_payload(case_id: str, mock_summary: dict[str, Any] | None) -> dict[str, Any]:
    summary = mock_summary or {}
    by_case = (summary.get("by_case") or {}).get(case_id) or {}
    unused = [
        item for item in (summary.get("unused_bindings") or [])
        if str(item.get("case_id") or "") == case_id
    ]
    return {
        "checked": bool(summary.get("checked")),
        "session_checked": bool(summary.get("sessions_checked")),
        "case_calls": by_case,
        "unused_bindings": unused[:10],
        "late_calls": [
            item for item in (summary.get("late_calls") or [])
            if str(item.get("case_id") or "") == case_id
        ][:10],
        "errors": [
            item for item in (summary.get("errors") or [])
            if str(item.get("case_id") or "") == case_id
        ][:10],
    }


def mock_semantics_payload(
    case: Any, mock_semantic: MockSemantic | None
) -> dict[str, Any]:
    """只回传该用例绑定到的下游分支，避免把整份语义库塞进提示词。"""
    if mock_semantic is None:
        return {}
    dep_index = mock_semantic.dep_index()
    payload: dict[str, Any] = {}
    for dep_id, bound_branch in (getattr(case, "mock_bindings", {}) or {}).items():
        dep = dep_index.get(str(dep_id))
        if dep is None:
            continue
        payload[str(dep_id)] = {
            "endpoint": dep.endpoint,
            "route_key": dep.route_key,
            "bound_branch": str(bound_branch),
            "branches": [
                {
                    "branch": branch.branch,
                    "semantics": branch.semantics,
                    "stub": branch.stub.model_dump(mode="json"),
                }
                for branch in dep.branches
            ],
        }
    return payload


def select_failures(
    entries: list[dict[str, Any]], max_failures: int = MAX_FAILURES
) -> list[dict[str, Any]]:
    """确定性归因已能定案的失败不再交给 LLM，节省一次调用。"""
    selected: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("outcome") not in ("failed", "error"):
            continue
        reason = str((entry.get("diagnosis") or {}).get("reason") or "")
        if reason not in ("unknown", "case_error", "change_induced", "sut_bug"):
            continue
        selected.append(entry)
        if len(selected) >= max_failures:
            break
    return selected


def diagnosis_payload(
    entry: dict[str, Any],
    case: Any,
    mock_summary: dict[str, Any] | None,
    changed_endpoints: set[str],
    *,
    case_key: str | None = None,
    mock_semantic: MockSemantic | None = None,
) -> dict[str, Any]:
    case_payload_value = case_payload(case, case_key=case_key)
    message = str(entry.get("message") or "")[:MAX_MESSAGE_CHARS]
    return {
        "case": case_payload_value,
        "execution": {
            "status": entry.get("outcome"),
            "message": message,
            "duration": entry.get("duration"),
            "attempts": entry.get("attempts") or [],
            "deterministic_diagnosis": entry.get("diagnosis") or {},
            "endpoint_changed": case_payload_value["endpoint"] in changed_endpoints,
        },
        "mock_evidence": mock_payload(str(entry.get("case_id") or ""), mock_summary),
        "mock_semantics": mock_semantics_payload(case, mock_semantic),
        "change_set": {
            "endpoint_changed": case_payload_value["endpoint"] in changed_endpoints,
        },
    }


def is_repairable(
    diagnosis: Any,
    entry: dict[str, Any],
    case: Any,
) -> bool:
    """只有高/中置信度、非 5xx、非正向超时的用例/ Mock 归因才允许自动修复。"""
    if str(getattr(diagnosis, "root_cause", "")) not in REPAIRABLE_ROOTS:
        return False
    if str(getattr(diagnosis, "action_target", "")) != ACTION_BY_ROOT[
        str(getattr(diagnosis, "root_cause", ""))
    ]:
        return False
    if str(getattr(diagnosis, "confidence", "")) not in ("high", "medium"):
        return False
    root = str(getattr(diagnosis, "root_cause", ""))
    reason = str((entry.get("diagnosis") or {}).get("reason") or "")
    if reason not in ("unknown", "case_error", "sut_bug"):
        return False
    message = str(entry.get("message") or "")
    # 5xx 只能由 Mock 语义错误解释：若把 5xx 当作用例错误去改断言，
    # 等于把真实代码缺陷“修绿”，必须拒绝。
    if _5XX_RE.search(message) and root != "mock_error":
        return False
    if root == "mock_error" and not (getattr(case, "mock_bindings", {}) or {}):
        return False
    if str(getattr(case, "category", "")) == "positive" and "timeout" in message.lower():
        return False
    return True


__all__ = [
    "ACTION_BY_ROOT",
    "MAX_FAILURES",
    "REPAIRABLE_ROOTS",
    "case_payload",
    "diagnosis_payload",
    "is_repairable",
    "mock_payload",
    "mock_semantics_payload",
    "select_failures",
]
