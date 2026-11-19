"""被删除接口的旧用例处置：自动逻辑删除，不进入运行。

delta 模式确认接口删除后调用。只处理 `approved` 且 transport 为 HTTP 的用例；
命中后标为 `deprecated`，`approved_cases` 与执行流程自然不再包含它。
落库前由 `save_case_set` 备份，`review_log` 以固定身份
`astsight:deleted-api` 记录，和人工审核区分。

扫描未完成（`checked=false`）或接口删除列表为空时不动用例库。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import (
    CaseReview,
    CaseStatus,
    ReviewDecision,
    ReviewEvent,
    Transport,
)

from ..profile.openapi import normalize_path
from .store import load_case_set, resolve_case_base, save_case_set

AUTOMATION_REVIEWER = "astsight:deleted-api"


def deprecate_deleted_api_cases(
    project: str | Path,
    case_base: str | Path | None,
    deleted_state: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """把指向已删除接口的 approved HTTP 用例标为 deprecated。"""
    result: dict[str, Any] = {
        "checked": False,
        "case_base": None,
        "deprecated": [],
        "warnings": [],
    }
    if not deleted_state.get("checked"):
        result["warnings"].append("deleted_api_cases_skipped:scan_not_checked")
        return result
    targets = {
        (str(item.get("method") or "").upper(), normalize_path(item.get("path")))
        for item in deleted_state.get("deleted") or []
        if item.get("method") and item.get("path")
    }
    if not targets:
        result["checked"] = True
        return result
    store_path = resolve_case_base(project, case_base)
    caseset = load_case_set(store_path)
    if caseset is None:
        result["warnings"].append("deleted_api_cases_skipped:case_store_missing")
        return result
    stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed: list[dict[str, Any]] = []
    for index, entry in enumerate(caseset.cases):
        if entry.status is not CaseStatus.APPROVED:
            continue
        if entry.transport is not Transport.HTTP:
            continue
        key = _endpoint_key(entry.endpoint)
        if key not in targets:
            continue
        note = (
            f"接口已删除：{key[0]} {key[1]}；"
            "自动逻辑删除，不进入运行"
        )
        updated = entry.model_copy(deep=True)
        updated.status = CaseStatus.DEPRECATED
        updated.updated_at = stamp
        updated.review = CaseReview(
            decision=ReviewDecision.APPROVE,
            by=AUTOMATION_REVIEWER,
            at=stamp,
            note=note,
            fingerprint=entry.fingerprint,
        )
        caseset.cases[index] = updated
        caseset.review_log.append(
            ReviewEvent(
                proposal_id=f"deleted-api:{key[0]} {key[1]}",
                case_id=entry.id,
                case_key=entry.case_key,
                action="deprecate",
                decision=ReviewDecision.APPROVE,
                reviewer=AUTOMATION_REVIEWER,
                at=stamp,
                note=note,
                fingerprint=entry.fingerprint,
            )
        )
        changed.append(
            {
                "id": entry.id,
                "case_key": entry.case_key,
                "endpoint": entry.endpoint,
                "normalized_endpoint": f"{key[0]} {key[1]}",
            }
        )
    if changed:
        caseset.store_version += 1
        caseset.updated_at = stamp
        save_case_set(store_path, caseset)
    result["checked"] = True
    result["case_base"] = str(store_path)
    result["deprecated"] = changed
    return result


def _endpoint_key(endpoint: str) -> tuple[str, str]:
    method, _, path = (endpoint or "").partition(" ")
    return method.upper(), normalize_path(path)


__all__ = ["AUTOMATION_REVIEWER", "deprecate_deleted_api_cases"]
