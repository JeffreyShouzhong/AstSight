"""被删除接口的旧用例处置：自动逻辑删除且不再进入运行。"""
from __future__ import annotations

from pathlib import Path

from astsight.cases.deleted_cleanup import (
    AUTOMATION_REVIEWER,
    deprecate_deleted_api_cases,
)
from astsight.cases.draft import case_fingerprint
from astsight.cases.store import (
    approved_cases,
    empty_case_set,
    load_case_set,
    save_case_set,
)
from contracts import (
    AssertStrength,
    Assertion,
    CaseEntry,
    CaseReview,
    CaseStatus,
    Priority,
    ReviewDecision,
    TestCase,
    Transport,
)

NOW = "2026-11-19T00:00:00+00:00"


def _entry(
    case_id: str,
    endpoint: str,
    case_key: str,
    *,
    status: CaseStatus = CaseStatus.APPROVED,
    transport: Transport = Transport.HTTP,
) -> CaseEntry:
    method, path = endpoint.split(" ", 1)
    case = TestCase(
        id=case_id,
        title=case_key,
        endpoint=endpoint,
        category="smoke",
        method=method,
        path=path,
        priority=Priority.P1,
        transport=transport,
        asserts=[Assertion(kind="status_code", op="eq", expect=200)],
        source="test",
        assert_strength=AssertStrength.L1,
    )
    fingerprint = case_fingerprint(case, case_key)
    return CaseEntry(
        **case.model_dump(),
        case_key=case_key,
        status=status,
        version=1,
        fingerprint=fingerprint,
        review=CaseReview(
            decision=ReviewDecision.APPROVE,
            by="alice",
            at=NOW,
            fingerprint=fingerprint,
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def _store(project: Path, entries: list[CaseEntry]):
    store = empty_case_set(project)
    store.cases = list(entries)
    store.sequence = max(int(entry.id.split("-")[1]) for entry in entries)
    save_case_set(project / "caseset.json", store)
    return store


def test_matching_cases_are_deprecated_and_excluded(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _store(
        project,
        [
            _entry("TC-0001", "GET /removed", "removed_case"),
            _entry("TC-0002", "GET /keep", "keep_case"),
        ],
    )
    deleted = {
        "checked": True,
        "deleted": [{"method": "GET", "path": "/removed"}],
    }
    result = deprecate_deleted_api_cases(project, None, deleted, now=NOW)
    assert result["checked"] is True
    assert [item["id"] for item in result["deprecated"]] == ["TC-0001"]

    store = load_case_set(project / "caseset.json")
    assert store is not None
    assert store.store_version == 1
    by_id = {entry.id: entry for entry in store.cases}
    assert by_id["TC-0001"].status is CaseStatus.DEPRECATED
    assert by_id["TC-0001"].review is not None
    assert by_id["TC-0001"].review.by == AUTOMATION_REVIEWER
    assert by_id["TC-0002"].status is CaseStatus.APPROVED
    assert [entry.id for entry in approved_cases(store)] == ["TC-0002"]
    assert store.review_log[-1].action == "deprecate"
    assert store.review_log[-1].reviewer == AUTOMATION_REVIEWER


def test_path_parameters_are_normalized(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _store(project, [_entry("TC-0001", "GET /orders/{id}", "order_detail")])
    deleted = {
        "checked": True,
        "deleted": [{"method": "GET", "path": "/orders/{id:int}"}],
    }
    result = deprecate_deleted_api_cases(project, None, deleted, now=NOW)
    assert [item["id"] for item in result["deprecated"]] == ["TC-0001"]


def test_unchecked_scan_does_not_touch_store(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _store(project, [_entry("TC-0001", "GET /removed", "removed_case")])
    result = deprecate_deleted_api_cases(
        project, None, {"checked": False, "deleted": []}, now=NOW
    )
    assert result["checked"] is False
    assert result["warnings"]
    store = load_case_set(project / "caseset.json")
    assert store is not None
    assert store.store_version == 0
    assert store.cases[0].status is CaseStatus.APPROVED


def test_already_deprecated_is_not_touched(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _store(
        project,
        [
            _entry(
                "TC-0001",
                "GET /removed",
                "removed_case",
                status=CaseStatus.DEPRECATED,
            )
        ],
    )
    result = deprecate_deleted_api_cases(
        project,
        None,
        {"checked": True, "deleted": [{"method": "GET", "path": "/removed"}]},
        now=NOW,
    )
    assert result["deprecated"] == []
    store = load_case_set(project / "caseset.json")
    assert store is not None
    assert store.store_version == 0


def test_non_http_cases_are_not_touched(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _store(
        project,
        [
            _entry(
                "TC-0001",
                "GET /removed",
                "removed_case",
                transport=Transport.DUBBO,
            )
        ],
    )
    result = deprecate_deleted_api_cases(
        project,
        None,
        {"checked": True, "deleted": [{"method": "GET", "path": "/removed"}]},
        now=NOW,
    )
    assert result["deprecated"] == []
    store = load_case_set(project / "caseset.json")
    assert store is not None
    assert store.cases[0].status is CaseStatus.APPROVED
