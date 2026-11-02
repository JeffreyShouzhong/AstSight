"""`mock_calls.jsonl` 证据写入与汇总。"""
from __future__ import annotations

import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from contracts import MockCall, MockCallResult

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_CALLS = 200_000
MAX_EVIDENCE_LINE_BYTES = 2 * 1024 * 1024
_BLOCKING_RESULTS = {
    MockCallResult.UNKNOWN,
    MockCallResult.AMBIGUOUS,
    MockCallResult.UNBOUND,
    MockCallResult.BRANCH_MISSING,
}


class MockEvidenceWriter:
    """线程安全的 JSONL 追加写入器；`path=None` 时只保留内存记录。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._records: list[MockCall] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, call: MockCall) -> None:
        with self._lock:
            self._records.append(call)
            if self.path is None:
                return
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(call.model_dump_json(exclude_none=True))
                handle.write("\n")

    @property
    def records(self) -> list[MockCall]:
        with self._lock:
            return list(self._records)


def read_evidence(
    path: str | Path,
    *,
    max_bytes: int = MAX_EVIDENCE_BYTES,
    max_calls: int = MAX_EVIDENCE_CALLS,
) -> tuple[list[MockCall], int]:
    """读取证据文件，返回 `(calls, invalid_lines)`。

    这是框架自己写的 JSONL，但服务可能在写到一半时被终止；单行损坏按
    `invalid_lines` 计数并由报告降级，不因为一行坏数据吞掉整批证据。
    文件/行/条目超限则抛错，报告显式标 `checked=false`。
    """
    evidence_path = Path(path)
    if not evidence_path.is_file():
        raise FileNotFoundError(f"mock evidence 不存在: {evidence_path}")
    size = evidence_path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"mock evidence 过大: {size} bytes > {max_bytes} bytes")

    calls: list[MockCall] = []
    invalid = 0
    with evidence_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_EVIDENCE_LINE_BYTES:
                raise ValueError(
                    f"mock evidence 第 {line_no} 行过大: "
                    f"{len(line)} chars > {MAX_EVIDENCE_LINE_BYTES} bytes"
                )
            try:
                calls.append(MockCall.model_validate_json(line))
            except ValueError:
                invalid += 1
                continue
            if len(calls) > max_calls:
                raise ValueError(
                    f"mock evidence 条目过多: > {max_calls}（第 {line_no} 行）"
                )
    return calls, invalid


def _session_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise TypeError(f"unsupported session record: {type(item).__name__}")


def _unused_bindings(
    calls: list[MockCall],
    sessions: list[Any],
    *,
    max_samples: int,
) -> tuple[int, list[dict[str, Any]]]:
    used: dict[str, set[str]] = {}
    for call in calls:
        if call.session_id and call.dep_id:
            used.setdefault(call.session_id, set()).add(call.dep_id)

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in sessions:
        session = _session_dict(raw)
        session_id = str(session.get("session_id") or "")
        case_id = str(session.get("case_id") or "")
        if not session_id:
            continue
        for binding in session.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            dep_id = str(binding.get("dep_id") or "")
            if not dep_id or dep_id in used.get(session_id, set()):
                continue
            key = (session_id, dep_id)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "session_id": session_id,
                    "case_id": case_id,
                    "dep_id": dep_id,
                    "branch": binding.get("branch"),
                    "created_at": session.get("created_at"),
                }
            )
    items.sort(key=lambda item: (item["case_id"], item["session_id"], item["dep_id"]))
    return len(items), items[:max_samples]


def _sample(call: MockCall, result: str) -> dict[str, Any]:
    return {
        "call_id": call.call_id,
        "case_id": call.case_id,
        "session_id": call.session_id,
        "dep_id": call.dep_id,
        "route_key": call.route_key,
        "route_match": call.route_match.value if call.route_match else None,
        "path": call.original_path,
        "result": result,
        "error": call.error,
        "at": call.at,
    }


def _unreadable_evidence(path: str | Path, exc: Exception) -> dict[str, Any]:
    return {
        "checked": False,
        "reason": f"{type(exc).__name__}: {exc}"[:500],
        "path": str(path),
        "calls": 0,
        "by_result": {},
        "by_case": {},
        "late_calls": [],
        "errors": [],
        "unattributed_calls": 0,
        "invalid_lines": 0,
        "evidence_missing": False,
        "sessions_checked": False,
        "session_records": 0,
        "unused_binding_count": 0,
        "unused_bindings": [],
        "sessions_missing": 0,
        "session_ids_missing": [],
        "degraded": True,
    }


def summarize_evidence(
    path: str | Path | None,
    *,
    max_samples: int = 50,
    sessions: list[Any] | None = None,
    allow_missing: bool = False,
) -> dict[str, Any]:
    """把 `mock_calls.jsonl` 汇总成报告可消费的确定性摘要。

    `sessions` 来自 `GET /_admin/sessions`。传入后会检查每个会话绑定的
    `dep_id` 是否真的被调用；绑定但零调用的依赖记为 `unused_bindings`
    （`mock_not_used`），防止“用例通过但根本没走到下游 Mock”的假绿。

    `allow_missing` 只供内嵌 plan 使用：服务启动后若一次下游调用都没有，
    证据文件不会被创建，但会话历史仍然有效，应视为“零调用”而非“证据缺失”。
    """
    if path is None:
        return {
            "checked": False,
            "reason": "mock_evidence_not_configured",
            "path": None,
            "calls": 0,
            "by_result": {},
            "by_case": {},
            "late_calls": [],
            "errors": [],
            "unattributed_calls": 0,
            "invalid_lines": 0,
            "evidence_missing": False,
            "sessions_checked": False,
            "session_records": 0,
            "unused_binding_count": 0,
            "unused_bindings": [],
            "sessions_missing": 0,
            "session_ids_missing": [],
            "degraded": False,
        }
    missing = False
    try:
        calls, invalid = read_evidence(path)
    except FileNotFoundError as exc:
        if not allow_missing:
            return _unreadable_evidence(path, exc)
        calls, invalid, missing = [], 0, True
    except (OSError, ValueError) as exc:
        return _unreadable_evidence(path, exc)

    by_result: Counter[str] = Counter()
    by_case: dict[str, dict[str, Any]] = {}
    session_ids: set[str] = set()
    late_calls: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    unattributed = 0

    for call in calls:
        result = call.result.value
        by_result[result] += 1
        if call.session_id:
            session_ids.add(call.session_id)
        if not call.case_id:
            unattributed += 1
        else:
            entry = by_case.setdefault(
                call.case_id,
                {
                    "case_id": call.case_id,
                    "calls": 0,
                    "results": Counter(),
                    "sessions": set(),
                    "branches": set(),
                    "deps": set(),
                },
            )
            entry["calls"] += 1
            entry["results"][result] += 1
            if call.session_id:
                entry["sessions"].add(call.session_id)
            if call.branch:
                entry["branches"].add(call.branch)
            if call.dep_id:
                entry["deps"].add(call.dep_id)
        if result == MockCallResult.LATE.value and len(late_calls) < max_samples:
            late_calls.append(_sample(call, result))
        if (
            call.result in _BLOCKING_RESULTS
            and len(errors) < max_samples
        ):
            errors.append(_sample(call, result))

    rendered_cases = {
        case_id: {
            "case_id": item["case_id"],
            "calls": item["calls"],
            "results": dict(item["results"]),
            "sessions": sorted(item["sessions"]),
            "branches": sorted(item["branches"]),
            "deps": sorted(item["deps"]),
        }
        for case_id, item in sorted(by_case.items())
    }
    blocking = sum(by_result[item.value] for item in _BLOCKING_RESULTS)
    sessions_checked = sessions is not None
    unused_count, unused_samples = (
        _unused_bindings(calls, sessions, max_samples=max_samples)
        if sessions_checked else (0, [])
    )
    known_sessions = (
        {
            str(_session_dict(item).get("session_id") or "")
            for item in sessions
        }
        if sessions_checked else set()
    )
    known_sessions.discard("")
    missing_sessions = (
        sorted(session_ids - known_sessions) if sessions_checked else []
    )
    return {
        "checked": True,
        "path": str(path),
        "calls": len(calls),
        "sessions": len(session_ids),
        "by_result": dict(sorted(by_result.items())),
        "by_case": rendered_cases,
        "late_calls": late_calls,
        "errors": errors,
        "blocking_errors": blocking,
        "unattributed_calls": unattributed,
        "invalid_lines": invalid,
        "evidence_missing": missing,
        "sessions_checked": sessions_checked,
        "session_records": len(sessions) if sessions_checked else 0,
        "unused_binding_count": unused_count,
        "unused_bindings": unused_samples,
        "sessions_missing": len(missing_sessions),
        "session_ids_missing": missing_sessions[:max_samples],
        "degraded": bool(
            blocking
            or unused_count
            or missing_sessions
            or by_result[MockCallResult.LATE.value]
            or by_result[MockCallResult.OK.value] != len(calls)
            or invalid
        ),
    }


__all__ = [
    "MAX_EVIDENCE_BYTES",
    "MAX_EVIDENCE_CALLS",
    "MockEvidenceWriter",
    "read_evidence",
    "summarize_evidence",
]
