"""项目级版本化用例库 `caseset.json`。

只有审核通过、已写入 store 的用例才会被 `run --mode cases` 使用。
写库采用“备份 + 乐观版本校验 + canonical hash + 原子替换”。
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import CaseEntry, CaseLevel, CaseSet, CaseStatus, ReviewDecision

from ..persistence import load_json, save_json_atomic, sha256_json, sha256_text, sha256_file
from .draft import case_fingerprint

DEFAULT_CASE_BASE = "caseset.json"
SUPPORTED_STORE_SCHEMA = 2
SUPPORTED_FINGERPRINT_VERSION = 2
EMPTY_STORE_HASH = sha256_json(
    {"case_set": "empty", "schema_version": SUPPORTED_STORE_SCHEMA}
)
_BACKUP_LIMIT = 10
_CASE_ID_RE = re.compile(r"^TC-(\d{4,})$")


class CaseStoreError(RuntimeError):
    """case store 不满足版本/指纹/唯一性约束。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_case_base(project: str | Path, case_base: str | Path | None) -> Path:
    project_path = Path(project).resolve()
    raw = Path(case_base or DEFAULT_CASE_BASE)
    path = raw if raw.is_absolute() else project_path / raw
    if path.suffix.lower() != ".json":
        raise CaseStoreError(f"case store 必须是 .json 文件: {path}")
    if path.exists() and path.is_dir():
        raise CaseStoreError(f"case store 路径是目录: {path}")
    return path.resolve()


def empty_case_set(
    project: str | Path,
    *,
    context: dict[str, Any] | None = None,
) -> CaseSet:
    project_path = Path(project).resolve()
    now = _now()
    return CaseSet(
        id="cs-" + sha256_text(project_path.as_posix())[:16],
        project=project_path.name,
        store_version=0,
        sequence=0,
        generated_at=now,
        updated_at=now,
        approved_at=None,
        approved_by=None,
        openapi=dict((context or {}).get("openapi") or {}),
        cases=[],
        history=[],
        review_log=[],
        # 空库不预填文档：只有批准时落在 scope 内的文档才进入覆盖率分母，
        # 避免通过一次 prepare 把所有文档都算进 store。
        doc_versions={},
        requirements=[],
        uncovered_reasons={},
    )


def _entry_fingerprint(entry: CaseEntry) -> str:
    return case_fingerprint(entry, entry.case_key)


def validate_case_set(caseset: CaseSet) -> None:
    if caseset.schema_version != SUPPORTED_STORE_SCHEMA:
        raise CaseStoreError(
            f"不支持的 case store schema_version={caseset.schema_version}"
        )
    if caseset.fingerprint_version != SUPPORTED_FINGERPRINT_VERSION:
        raise CaseStoreError(
            f"不支持的 fingerprint_version={caseset.fingerprint_version}"
        )
    snapshot = caseset.ruleset or {}
    if snapshot:
        missing = [
            key for key in ("id", "version", "hash") if not snapshot.get(key)
        ]
        if missing:
            raise CaseStoreError(
                f"caseset.ruleset 快照缺少字段: {missing}"
            )

    seen: dict[tuple[str, int], str] = {}
    anchors: dict[tuple[str, str], str] = {}
    max_number = 0
    for entry in caseset.cases:
        if entry.status not in {CaseStatus.APPROVED, CaseStatus.DEPRECATED}:
            raise CaseStoreError(
                f"{entry.id} 在 cases 中状态非法: {entry.status.value}"
            )
        anchor = (entry.endpoint, entry.case_key)
        if anchor in anchors:
            raise CaseStoreError(
                f"cases 中存在重复锚点: {entry.endpoint} / {entry.case_key}"
            )
        anchors[anchor] = entry.id
    for entry in caseset.history:
        if entry.status is not CaseStatus.SUPERSEDED:
            raise CaseStoreError(
                f"{entry.id} 在 history 中状态必须是 superseded，实际 {entry.status.value}"
            )
    for entry in list(caseset.cases) + list(caseset.history):
        match = _CASE_ID_RE.match(entry.id)
        if not match:
            raise CaseStoreError(f"非法 TC 编号: {entry.id!r}")
        max_number = max(max_number, int(match.group(1)))
        key = (entry.id, entry.version)
        if key in seen:
            raise CaseStoreError(
                f"重复的 (id, version): {entry.id} / v{entry.version}"
            )
        seen[key] = entry.case_key
        if entry.fingerprint_version != SUPPORTED_FINGERPRINT_VERSION:
            raise CaseStoreError(
                f"{entry.id} 不支持的 fingerprint_version={entry.fingerprint_version}"
            )
        actual = _entry_fingerprint(entry)
        if actual != entry.fingerprint:
            raise CaseStoreError(
                f"{entry.id} v{entry.version} 指纹不匹配：存储={entry.fingerprint} 重算={actual}"
            )
        if entry.review and entry.review.fingerprint != entry.fingerprint:
            raise CaseStoreError(
                f"{entry.id} v{entry.version} 审核指纹与用例指纹不一致"
            )
        if entry.review and entry.review.decision is not ReviewDecision.APPROVE:
            raise CaseStoreError(
                f"{entry.id} v{entry.version} 已入库但审核决定不是 approve"
            )
        if snapshot:
            _validate_level_invariants(entry)
    if caseset.sequence < max_number:
        raise CaseStoreError(
            f"sequence={caseset.sequence} 小于最大 TC 编号 {max_number}"
        )


def _validate_level_invariants(entry: CaseEntry) -> None:
    """只有规则驱动的 store 强制两级不变量；模板回归用例不伪造 rule_refs。"""
    if not entry.rule_refs:
        raise CaseStoreError(f"{entry.id} 缺少 rule_refs（规则驱动 use store 必填）")
    if entry.level is CaseLevel.BUSINESS and not entry.requirement_refs:
        raise CaseStoreError(f"{entry.id} business 用例缺少 requirement_refs")
    if entry.level is CaseLevel.CODE and not entry.change_refs:
        raise CaseStoreError(f"{entry.id} code 用例缺少 change_refs")


def load_case_set(path: str | Path) -> CaseSet | None:
    store_path = Path(path)
    if not store_path.is_file():
        return None
    data = load_json(store_path)
    try:
        caseset = CaseSet.model_validate(data)
    except Exception as exc:
        raise CaseStoreError(f"case store 解析失败: {store_path}: {exc}") from exc
    validate_case_set(caseset)
    return caseset


def store_hash(caseset: CaseSet) -> str:
    return sha256_json(caseset.model_dump(mode="json"))


def case_number(case_id: str) -> int | None:
    match = _CASE_ID_RE.match(case_id or "")
    return int(match.group(1)) if match else None


def max_case_number(caseset: CaseSet) -> int:
    numbers = [
        number
        for number in (
            case_number(entry.id)
            for entry in list(caseset.cases) + list(caseset.history)
        )
        if number is not None
    ]
    return max(numbers or [0])


def allocate_case_id(caseset: CaseSet) -> str:
    """从 cases + history 的最大编号递增；sequence 只增不减。"""
    high = max(caseset.sequence, max_case_number(caseset))
    next_number = high + 1
    caseset.sequence = next_number
    return f"TC-{next_number:04d}"


def approved_cases(caseset: CaseSet) -> list[CaseEntry]:
    return [
        entry for entry in caseset.cases if entry.status is CaseStatus.APPROVED
    ]


def find_case(
    caseset: CaseSet, *, endpoint: str, case_key: str
) -> CaseEntry | None:
    for entry in caseset.cases:
        if entry.endpoint == endpoint and entry.case_key == case_key:
            return entry
    return None


def case_summary(caseset: CaseSet) -> dict[str, int]:
    approved = sum(
        1 for entry in caseset.cases if entry.status is CaseStatus.APPROVED
    )
    deprecated = sum(
        1 for entry in caseset.cases if entry.status is CaseStatus.DEPRECATED
    )
    levels = {"business": 0, "code": 0}
    for entry in caseset.cases:
        if entry.status is CaseStatus.APPROVED:
            levels[entry.level.value] = levels.get(entry.level.value, 0) + 1
    return {
        "approved": approved,
        "deprecated": deprecated,
        "history_versions": len(caseset.history),
        "total_cases": len(caseset.cases),
        "review_events": len(caseset.review_log),
        "business_cases": levels["business"],
        "code_cases": levels["code"],
        "has_ruleset": bool(caseset.ruleset),
    }


def requirement_coverage(caseset: CaseSet) -> dict[str, Any]:
    total_sections: dict[tuple[str, str], None] = {}
    for requirement in caseset.requirements:
        for section in requirement.sections:
            total_sections[(requirement.doc, section)] = None
    covered: set[tuple[str, str]] = set()
    for entry in approved_cases(caseset):
        for ref in entry.requirement_refs:
            if ref.doc and ref.section:
                covered.add((ref.doc, ref.section))
    uncovered = []
    for doc, section in total_sections:
        if (doc, section) in covered:
            continue
        key = f"{doc}::{section}"
        uncovered.append(
            {
                "doc": doc,
                "section": section,
                "reason": caseset.uncovered_reasons.get(key, "no_case"),
            }
        )
    total = len(total_sections)
    return {
        "total": total,
        "covered": len(covered & set(total_sections)),
        "rate": round(len(covered & set(total_sections)) / total, 4) if total else 1.0,
        "uncovered": sorted(uncovered, key=lambda item: (item["doc"], item["section"])),
    }


def requirement_drift(
    caseset: CaseSet, project: str | Path
) -> dict[str, Any]:
    project_path = Path(project).resolve()
    changed: list[dict[str, str]] = []
    missing: list[str] = []
    for doc, expected in caseset.doc_versions.items():
        path = project_path / doc
        if not path.is_file():
            missing.append(doc)
            continue
        actual = sha256_file(path)
        if actual != expected:
            changed.append({"doc": doc, "expected": expected, "actual": actual})
    return {
        "checked": bool(caseset.doc_versions),
        "changed": bool(changed),
        "changed_docs": changed,
        "missing_docs": missing,
    }


def _backup_dir(store_path: Path) -> Path:
    return store_path.parent / ".astsight" / "cases" / "backups"


def backup_store(store_path: str | Path, caseset: CaseSet) -> Path | None:
    path = Path(store_path)
    if not path.is_file():
        return None
    target_dir = _backup_dir(path)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = target_dir / f"caseset-v{caseset.store_version}-{stamp}.json"
    shutil.copy2(path, target)
    backups = sorted(
        target_dir.glob("caseset-v*.json"), key=lambda item: item.stat().st_mtime
    )
    for old in backups[:-_BACKUP_LIMIT]:
        old.unlink(missing_ok=True)
    return target


def save_case_set(
    path: str | Path,
    caseset: CaseSet,
    *,
    backup: bool = True,
) -> Path | None:
    validate_case_set(caseset)
    store_path = Path(path)
    backup_path = backup_store(store_path, caseset) if backup else None
    save_json_atomic(store_path, caseset.model_dump(mode="json"))
    return backup_path


def revision_matches(
    caseset: CaseSet, expected_version: int, expected_hash: str
) -> bool:
    return (
        caseset.store_version == expected_version
        and store_hash(caseset) == expected_hash
    )


__all__ = [
    "CaseStoreError",
    "DEFAULT_CASE_BASE",
    "EMPTY_STORE_HASH",
    "SUPPORTED_FINGERPRINT_VERSION",
    "SUPPORTED_STORE_SCHEMA",
    "allocate_case_id",
    "approved_cases",
    "backup_store",
    "case_number",
    "case_summary",
    "empty_case_set",
    "find_case",
    "load_case_set",
    "max_case_number",
    "requirement_coverage",
    "requirement_drift",
    "resolve_case_base",
    "revision_matches",
    "save_case_set",
    "store_hash",
    "validate_case_set",
]
