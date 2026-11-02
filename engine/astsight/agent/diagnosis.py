"""A5 失败诊断：确定性归因之后的疑难失败兜底。

LLM 只解释失败，不重试、不改用例、不改审核决定。确定性归因保持原样，
LLM 结论单独存放；只有 `case_error` + 高置信度才允许进入修复建议阶段。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm import LLMClient, LLMError
from .errors import AgentError
from .models import FailureDiagnosis

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "a5_result_diagnosis.md"
MAX_FAILURES = 20
MAX_MESSAGE_CHARS = 8_000
_SECRET_HEADERS = ("authorization", "cookie", "token", "secret", "api-key", "x-api-key")
_5XX_RE = re.compile(r"\b5\d{2}\b")
_ACTION_BY_ROOT = {
    "code_defect": "user",
    "case_error": "user",
    "change_induced": "user",
    "data_error": "framework",
    "mock_error": "framework",
    "adapter_error": "framework",
    "env_error": "framework",
}


@dataclass
class DiagnosisOutcome:
    by_case: dict[str, dict[str, Any]] = field(default_factory=dict)
    eligible_case_ids: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_diagnosis_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else _PROMPT_PATH
    if not prompt_path.is_file():
        raise AgentError("diagnosis_prompt_missing", str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if any(token in str(key).lower() for token in _SECRET_HEADERS):
            safe[str(key)] = "***"
        else:
            safe[str(key)] = str(value)[:500]
    return safe


def _case_payload(case: Any) -> dict[str, Any]:
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
        "case_key": getattr(case, "id", ""),
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
        "requirement_refs": refs,
        "mock_bindings": dict(getattr(case, "mock_bindings", {}) or {}),
        "source": getattr(case, "source", ""),
    }


def _mock_payload(case_id: str, mock_summary: dict[str, Any] | None) -> dict[str, Any]:
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


def _select(entries: list[dict[str, Any]], max_failures: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("outcome") not in ("failed", "error"):
            continue
        reason = str((entry.get("diagnosis") or {}).get("reason") or "")
        if reason not in ("unknown", "case_error", "change_induced"):
            continue
        selected.append(entry)
        if len(selected) >= max_failures:
            break
    return selected


def _diagnosis_payload(
    entry: dict[str, Any],
    case: Any,
    mock_summary: dict[str, Any] | None,
    changed_endpoints: set[str],
) -> dict[str, Any]:
    case_payload = _case_payload(case)
    message = str(entry.get("message") or "")[:MAX_MESSAGE_CHARS]
    return {
        "case": case_payload,
        "execution": {
            "status": entry.get("outcome"),
            "message": message,
            "duration": entry.get("duration"),
            "attempts": entry.get("attempts") or [],
            "deterministic_diagnosis": entry.get("diagnosis") or {},
            "endpoint_changed": case_payload["endpoint"] in changed_endpoints,
        },
        "mock_evidence": _mock_payload(str(entry.get("case_id") or ""), mock_summary),
        "change_set": {
            "endpoint_changed": case_payload["endpoint"] in changed_endpoints,
        },
    }


def _eligible(
    diagnosis: FailureDiagnosis,
    entry: dict[str, Any],
    case: Any,
) -> bool:
    if diagnosis.root_cause != "case_error":
        return False
    if diagnosis.action_target != "user":
        return False
    if diagnosis.confidence not in ("high", "medium"):
        return False
    reason = str((entry.get("diagnosis") or {}).get("reason") or "")
    if reason not in ("unknown", "case_error"):
        return False
    message = str(entry.get("message") or "")
    if _5XX_RE.search(message):
        return False
    if str(getattr(case, "category", "")) == "positive" and "timeout" in message.lower():
        return False
    return True


def diagnose_failures(
    *,
    cases: list[Any],
    entries: list[dict[str, Any]],
    llm: LLMClient,
    mock_summary: dict[str, Any] | None = None,
    changed_endpoints: set[str] | None = None,
    max_failures: int = MAX_FAILURES,
    prompt_path: str | Path | None = None,
) -> DiagnosisOutcome:
    cases_by_id = {str(getattr(case, "id", "")): case for case in cases}
    selected = [entry for entry in _select(entries, max_failures) if entry.get("case_id") in cases_by_id]
    outcome = DiagnosisOutcome()
    if not selected:
        return outcome

    changed = changed_endpoints or set()
    packages = [
        _diagnosis_payload(
            entry,
            cases_by_id[str(entry["case_id"])],
            mock_summary,
            changed,
        )
        for entry in selected
    ]
    system = load_diagnosis_prompt(prompt_path)
    user = json.dumps(
        {
            "diagnoses": packages,
            "note": "确定性归因已先行；请只对这些疑难失败给出根因假设，不要改写用例。",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("diagnosis_llm_failed", str(exc)) from exc
    items = payload if isinstance(payload, list) else [payload]
    requested = {str(entry["case_id"]) for entry in selected}
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            outcome.errors.append("diagnosis_invalid:非 JSON object")
            continue
        try:
            diagnosis = FailureDiagnosis.model_validate(raw)
        except Exception as exc:
            outcome.errors.append(f"diagnosis_invalid:{str(exc)[:300]}")
            continue
        if diagnosis.case_id not in requested:
            outcome.errors.append(f"diagnosis_unknown_case:{diagnosis.case_id}")
            continue
        if diagnosis.case_id in seen:
            outcome.warnings.append(f"diagnosis_duplicate_case:{diagnosis.case_id}")
            continue
        seen.add(diagnosis.case_id)
        expected_target = _ACTION_BY_ROOT[diagnosis.root_cause]
        if diagnosis.action_target != expected_target:
            outcome.warnings.append(
                f"diagnosis_action_target_corrected:{diagnosis.case_id}:"
                f"{diagnosis.action_target}->{expected_target}"
            )
            diagnosis = diagnosis.model_copy(update={"action_target": expected_target})
        entry = next(item for item in selected if item["case_id"] == diagnosis.case_id)
        case = cases_by_id[diagnosis.case_id]
        record = diagnosis.model_dump(mode="json")
        record["eligible_for_repair"] = _eligible(diagnosis, entry, case)
        outcome.by_case[diagnosis.case_id] = record
        if record["eligible_for_repair"]:
            outcome.eligible_case_ids.add(diagnosis.case_id)
    missing = requested - seen
    for case_id in sorted(missing):
        outcome.warnings.append(f"diagnosis_missing:{case_id}")
    return outcome


__all__ = [
    "DiagnosisOutcome",
    "diagnose_failures",
    "load_diagnosis_prompt",
]
