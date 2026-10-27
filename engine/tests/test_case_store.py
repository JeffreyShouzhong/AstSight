from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from astsight.cases.context import build_context
from astsight.cases.draft import (
    CaseDraft,
    DraftCase,
    case_fingerprint,
    load_draft,
)
from astsight.cases.proposal import (
    ProposalError,
    approve_proposal,
    build_proposal,
    classify,
    list_pending_proposals,
)
from astsight.cases.store import (
    CaseStoreError,
    allocate_case_id,
    case_summary,
    empty_case_set,
    load_case_set,
    requirement_coverage,
    save_case_set,
    store_hash,
    validate_case_set,
)
from astsight.cases.validate import ResolvedDraftCase, ValidationResult, validate_draft
from contracts import (
    AssertStrength,
    Assertion,
    CaseEntry,
    CaseReview,
    CaseSet,
    CaseStatus,
    Priority,
    RequirementDoc,
    ReviewDecision,
    SourceRef,
    TestCase as CaseModel,
)

DOC = """# 订单 API

## POST /orders

创建订单。quantity 必须大于等于 1。

## PATCH /orders/{order_id}

状态只能是 pending 或 paid。
"""

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "demo", "version": "1.0"},
    "paths": {
        "/orders": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["quantity"],
                                "properties": {"quantity": {"type": "integer"}},
                            }
                        }
                    }
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"order_id": {"type": "integer"}},
                                }
                            }
                        },
                    },
                    "422": {"description": "invalid"},
                },
            }
        },
        "/orders/{order_id}": {
            "patch": {
                "parameters": [
                    {"name": "order_id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["status"],
                                "properties": {"status": {"type": "string"}},
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _project(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text(DOC, encoding="utf-8")
    (tmp_path / "openapi.json").write_text(json.dumps(SPEC), encoding="utf-8")
    return tmp_path


def _test_case(
    case_key: str,
    *,
    endpoint: str = "POST /orders",
    body: Any = None,
    docs: tuple[str, ...] = ("docs/api.md",),
    section: str = "订单 API > POST /orders",
    case_id: str = "TC-0000",
) -> CaseModel:
    method, path = endpoint.split(" ", 1)
    return CaseModel(
        id=case_id,
        title=case_key,
        endpoint=endpoint,
        category="business",
        method=method,
        path=path,
        priority=Priority.P0,
        path_params={"order_id": 1} if "{order_id}" in path else {},
        requirement_refs=[
            SourceRef(doc=doc, section=section, quote="quantity 必须大于等于 1")
            for doc in docs
        ],
        body=body if body is not None else {"quantity": 1},
        asserts=[Assertion(kind="status_code", op="eq", expect=201)],
        source="requirement:docs/api.md#s",
        assert_strength=AssertStrength.L1,
    )


def _entry(
    case_key: str,
    *,
    case_id: str = "TC-0001",
    version: int = 1,
    status: CaseStatus = CaseStatus.APPROVED,
    endpoint: str = "POST /orders",
    body: Any = None,
    docs: tuple[str, ...] = ("docs/api.md",),
    section: str = "订单 API > POST /orders",
) -> CaseEntry:
    case = _test_case(
        case_key,
        endpoint=endpoint,
        body=body,
        docs=docs,
        section=section,
        case_id=case_id,
    )
    fingerprint = case_fingerprint(case, case_key)
    return CaseEntry(
        **case.model_dump(),
        case_key=case_key,
        status=status,
        version=version,
        fingerprint=fingerprint,
        review=CaseReview(
            decision=ReviewDecision.APPROVE,
            by="alice",
            at="2026-01-01T00:00:00+00:00",
            fingerprint=fingerprint,
        ),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _store_with(entries: list[CaseEntry], *, history: list[CaseEntry] | None = None) -> CaseSet:
    store = CaseSet(
        id="cs-test",
        project="demo",
        store_version=1,
        sequence=max(
            [
                int(entry.id.split("-")[1])
                for entry in entries + (history or [])
            ]
            or [0]
        ),
        generated_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        cases=entries,
        history=history or [],
        doc_versions={"docs/api.md": "v1"},
        requirements=[
            RequirementDoc(
                doc="docs/api.md", version="v1", sections=["订单 API > POST /orders"]
            )
        ],
    )
    return store


def _resolved(
    case_key: str,
    *,
    endpoint: str = "POST /orders",
    body: Any = None,
    docs: tuple[str, ...] = ("docs/api.md",),
    section: str = "订单 API > POST /orders",
) -> ResolvedDraftCase:
    case = _test_case(
        case_key,
        endpoint=endpoint,
        body=body,
        docs=docs,
        section=section,
    )
    draft_case = DraftCase(
        case_key=case_key,
        title=case_key,
        requirement_refs=[
            {
                "doc": doc,
                "section": section,
                "quote": "quantity 必须大于等于 1",
            }
            for doc in docs
        ],
        endpoint=endpoint,
        category="business",
        priority=Priority.P0,
        path_params=case.path_params,
        body=case.body,
        asserts=case.asserts,
    )
    return ResolvedDraftCase(
        draft_case=draft_case,
        case=case,
        fingerprint=case_fingerprint(case, case_key),
        profile={"endpoint": endpoint, "method": case.method, "path": case.path},
    )


def _validation(resolved: list[ResolvedDraftCase], scope: list[str]) -> ValidationResult:
    return ValidationResult(
        context_id="ctx",
        resolved=resolved,
        scope_docs=scope,
        requirements=[
            RequirementDoc(
                doc="docs/api.md", version="v1", sections=["订单 API > POST /orders"]
            )
        ],
    )


def _draft_case(case_key: str, *, body: Any = None, endpoint: str = "POST /orders") -> DraftCase:
    return DraftCase(
        case_key=case_key,
        title=case_key,
        requirement_refs=[
            {
                "doc": "docs/api.md",
                "section": "订单 API > POST /orders",
                "quote": "quantity 必须大于等于 1",
            }
        ],
        endpoint=endpoint,
        category="business",
        priority=Priority.P0,
        path_params={"order_id": 1} if "{order_id}" in endpoint else {},
        body=body if body is not None else {"quantity": 1},
        asserts=[{"kind": "status_code", "op": "eq", "expect": 201}],
    )


def _case_draft(context: dict, cases: list[DraftCase], **overrides) -> CaseDraft:
    payload = {
        "schema_version": 1,
        "context_id": context["context_id"],
        "generator": {"kind": "manual", "name": "test"},
        "scope_docs": ["docs/api.md"],
        "cases": cases,
        "uncovered_requirements": [],
    }
    payload.update(overrides)
    return CaseDraft.model_validate(payload)


def _import(
    project: Path,
    context: dict,
    draft_path: Path,
    draft: CaseDraft,
    store: CaseSet,
    store_path: Path,
):
    validation = validate_draft(draft, context, project=project)
    assert not validation.errors, [item.model_dump() for item in validation.errors]
    return build_proposal(
        project=project,
        draft_path=draft_path,
        draft=draft,
        context=context,
        validation=validation,
        store=store,
        store_path=store_path,
    )


def _review(proposal_path: Path, decisions: dict[str, str], note: str | None = None) -> None:
    review_path = proposal_path / "cases.review.json"
    data = json.loads(review_path.read_text(encoding="utf-8"))
    for item in data["items"]:
        decision = decisions.get(item["proposal_key"], "approve")
        item["decision"] = decision
        item["reviewer"] = "alice"
        item["note"] = note
    review_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _prepare_imported(project: Path, tmp_path: Path) -> tuple[dict, Path, Path]:
    context = build_context(project, ["docs/api.md"])
    draft_path = tmp_path / "cases.draft.json"
    draft_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "context_id": context["context_id"],
                "scope_docs": ["docs/api.md"],
                "cases": [
                    _draft_case("create_order_min_quantity").model_dump(mode="json")
                ],
            }
        ),
        encoding="utf-8",
    )
    return context, draft_path, tmp_path / "caseset.json"


# ───────────────────────── 指纹/存储不变量 ─────────────────────────


def test_fingerprint_ignores_non_execution_fields() -> None:
    case = _test_case("case_a", body={"quantity": 1})
    fingerprint = case_fingerprint(case, "case_a")
    for attr, value in (
        ("id", "TC-9999"),
        ("title", "改名"),
        ("source", "skill:other"),
        ("note", "备注"),
        ("rationale", "理由"),
    ):
        changed = case.model_copy(deep=True)
        setattr(changed, attr, value)
        assert case_fingerprint(changed, "case_a") == fingerprint

    changed = case.model_copy(deep=True)
    changed.requirement_refs[0].version = "other-version"
    assert case_fingerprint(changed, "case_a") == fingerprint

    changed = case.model_copy(deep=True)
    changed.body = {"quantity": 2}
    assert case_fingerprint(changed, "case_a") != fingerprint


def test_store_validates_id_version_sequence_and_review() -> None:
    entry = _entry("case_a")
    store = _store_with([entry])
    validate_case_set(store)

    duplicate = store.model_copy(deep=True)
    duplicate_history = entry.model_copy(deep=True)
    duplicate_history.status = CaseStatus.SUPERSEDED
    duplicate.history.append(duplicate_history)
    with pytest.raises(CaseStoreError, match="重复的 \\(id, version\\)"):
        validate_case_set(duplicate)

    low_sequence = store.model_copy(deep=True)
    low_sequence.sequence = 0
    with pytest.raises(CaseStoreError, match="sequence"):
        validate_case_set(low_sequence)

    bad_review = store.model_copy(deep=True)
    bad_review.cases[0].review = CaseReview(
        decision=ReviewDecision.APPROVE,
        fingerprint="wrong",
    )
    with pytest.raises(CaseStoreError, match="审核指纹"):
        validate_case_set(bad_review)

    bad_fingerprint = store.model_copy(deep=True)
    bad_fingerprint.cases[0].fingerprint = "wrong"
    with pytest.raises(CaseStoreError, match="指纹不匹配"):
        validate_case_set(bad_fingerprint)

    history_wrong_status = store.model_copy(deep=True)
    history_wrong_status.history.append(
        _entry("old", case_id="TC-0002", version=1, status=CaseStatus.APPROVED)
    )
    with pytest.raises(CaseStoreError, match="history 中状态"):
        validate_case_set(history_wrong_status)


def test_case_ids_are_not_reused_after_deprecation() -> None:
    store = _store_with(
        [_entry("case_a", case_id="TC-0003", status=CaseStatus.DEPRECATED)]
    )
    assert allocate_case_id(store) == "TC-0004"
    assert store.sequence == 4
    store.sequence = 9
    assert allocate_case_id(store) == "TC-0010"
    assert store.sequence == 10


def test_save_load_and_backup(tmp_path: Path) -> None:
    store_path = tmp_path / "caseset.json"
    store = _store_with([_entry("case_a")])
    save_case_set(store_path, store, backup=False)
    loaded = load_case_set(store_path)
    assert loaded is not None
    assert store_hash(loaded) == store_hash(store)

    loaded.approved_by = "bob"
    backup = save_case_set(store_path, loaded)
    assert backup is not None and backup.is_file()
    assert load_case_set(store_path).approved_by == "bob"
    backups = list((tmp_path / ".astsight" / "cases" / "backups").glob("*.json"))
    assert len(backups) == 1


def test_requirement_coverage_only_counts_approved() -> None:
    store = _store_with(
        [
            _entry("covered", case_id="TC-0001"),
            _entry(
                "deprecated",
                case_id="TC-0002",
                status=CaseStatus.DEPRECATED,
                section="订单 API > PATCH /orders/{order_id}",
            ),
        ]
    )
    store.requirements[0].sections = [
        "订单 API > POST /orders",
        "订单 API > PATCH /orders/{order_id}",
    ]
    store.uncovered_reasons = {
        "docs/api.md::订单 API > PATCH /orders/{order_id}": "needs_state"
    }
    coverage = requirement_coverage(store)
    assert coverage["total"] == 2
    assert coverage["covered"] == 1
    assert coverage["rate"] == 0.5
    assert coverage["uncovered"] == [
        {
            "doc": "docs/api.md",
            "section": "订单 API > PATCH /orders/{order_id}",
            "reason": "needs_state",
        }
    ]
    assert case_summary(store)["approved"] == 1
    assert case_summary(store)["deprecated"] == 1


def test_classify_six_way_diff() -> None:
    update_old = _entry("update_me", case_id="TC-0001", body={"quantity": 1})
    unchanged = _entry("unchanged_me", case_id="TC-0002", body={"quantity": 1})
    deprecated = _entry(
        "revive_me", case_id="TC-0003", status=CaseStatus.DEPRECATED
    )
    deprecate = _entry("drop_me", case_id="TC-0004")
    out_of_scope = _entry(
        "keep_me", case_id="TC-0005", docs=("docs/other.md",)
    )
    no_refs = _entry("no_refs", case_id="TC-0006", docs=())
    store = _store_with([update_old, unchanged, deprecated, deprecate, out_of_scope, no_refs])
    resolved = [
        _resolved("update_me", body={"quantity": 2}),
        _resolved("unchanged_me", body={"quantity": 1}),
        _resolved("revive_me", body={"quantity": 1}),
        _resolved("brand_new", body={"quantity": 3}),
    ]
    diff = classify(
        _validation(resolved, ["docs/api.md"]),
        store,
        scope_docs=["docs/api.md"],
    )
    actions = {(item.endpoint, item.case_key): item.action for item in diff.items}
    assert actions[("POST /orders", "update_me")] == "update"
    assert actions[("POST /orders", "revive_me")] == "revive"
    assert actions[("POST /orders", "brand_new")] == "new"
    assert actions[("POST /orders", "drop_me")] == "deprecate"
    assert [item["case_key"] for item in diff.unchanged] == ["unchanged_me"]
    kept = {item["case_key"] for item in diff.kept_out_of_scope}
    assert kept == {"keep_me", "no_refs"}
    assert diff.counts() == {
        "new": 1,
        "update": 1,
        "revive": 1,
        "deprecate": 1,
        "unchanged": 1,
        "kept_out_of_scope": 2,
    }


# ───────────────────────── proposal / 批准 ─────────────────────────


def test_proposal_approval_history_versions_and_idempotency(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context, draft_path, store_path = _prepare_imported(project, tmp_path)
    draft = load_draft(draft_path)
    store = empty_case_set(project, context=context)
    result = _import(project, context, draft_path, draft, store, store_path)
    assert result.path is not None

    # 未审核不能落库
    with pytest.raises(ProposalError, match="未审核"):
        approve_proposal(
            proposal_path=result.path,
            project=project,
            case_base=store_path,
            reviewer="alice",
        )
    assert not store_path.exists()

    _review(result.path, {"POST /orders::create_order_min_quantity": "approve"})
    approval = approve_proposal(
        proposal_path=result.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    assert approval["store_version"] == 1
    first = load_case_set(store_path)
    assert first is not None
    assert first.cases[0].id == "TC-0001"
    assert first.cases[0].version == 1
    assert first.cases[0].status is CaseStatus.APPROVED

    # 幂等：重复 approve 不再升版本
    again = approve_proposal(
        proposal_path=result.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    assert again == approval
    assert load_case_set(store_path).store_version == 1

    # 修改后重新 import → update，旧版本进 history，编号不变
    updated = _case_draft(
        context,
        [_draft_case("create_order_min_quantity", body={"quantity": 2})],
    )
    updated_path = tmp_path / "cases.draft.json"
    updated_path.write_text(
        json.dumps(updated.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    result2 = _import(project, context, updated_path, updated, first, store_path)
    assert result2.diff.counts()["update"] == 1
    _review(result2.path, {"POST /orders::create_order_min_quantity": "approve"})
    approve_proposal(
        proposal_path=result2.path,
        project=project,
        case_base=store_path,
        reviewer="bob",
    )
    second = load_case_set(store_path)
    assert second.store_version == 2
    assert second.cases[0].id == "TC-0001"
    assert second.cases[0].version == 2
    assert second.cases[0].review.by == "bob"
    assert len(second.history) == 1
    assert second.history[0].status is CaseStatus.SUPERSEDED
    assert second.history[0].version == 1
    assert second.sequence == 1

    # 新增另一条 → 新编号，不复用
    third_draft = _case_draft(
        context,
        [
            _draft_case("create_order_min_quantity", body={"quantity": 2}),
            _draft_case("create_order_quantity_two", body={"quantity": 3}),
        ],
    )
    third_path = tmp_path / "cases.draft.json"
    third_path.write_text(
        json.dumps(third_draft.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    result3 = _import(project, context, third_path, third_draft, second, store_path)
    assert result3.diff.counts()["new"] == 1
    new_key = "POST /orders::create_order_quantity_two"
    _review(result3.path, {new_key: "approve"})
    approve_proposal(
        proposal_path=result3.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    third = load_case_set(store_path)
    ids = {entry.case_key: entry.id for entry in third.cases}
    assert ids["create_order_min_quantity"] == "TC-0001"
    assert ids["create_order_quantity_two"] == "TC-0002"
    assert third.sequence == 2


def test_reject_keeps_store_and_pending_blocks_everything(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context, draft_path, store_path = _prepare_imported(project, tmp_path)
    store = empty_case_set(project, context=context)
    draft = load_draft(draft_path)
    first = _import(project, context, draft_path, draft, store, store_path)
    _review(first.path, {"POST /orders::create_order_min_quantity": "approve"})
    approve_proposal(
        proposal_path=first.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    approved = load_case_set(store_path)

    # 草案只剩另一条，旧用例会被提议 deprecate
    second_draft = _case_draft(
        context,
        [_draft_case("create_order_quantity_two", body={"quantity": 3})],
    )
    second_path = tmp_path / "cases.draft.json"
    second_path.write_text(
        json.dumps(second_draft.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    second = _import(project, context, second_path, second_draft, approved, store_path)
    actions = {item.proposal_key: item.action for item in second.diff.items}
    assert actions["POST /orders::create_order_min_quantity"] == "deprecate"

    _review(
        second.path,
        {
            "POST /orders::create_order_quantity_two": "approve",
            "POST /orders::create_order_min_quantity": "reject",
        },
    )
    approve_proposal(
        proposal_path=second.path,
        project=project,
        case_base=store_path,
        reviewer="bob",
    )
    after = load_case_set(store_path)
    assert {entry.case_key for entry in after.cases} == {
        "create_order_min_quantity",
        "create_order_quantity_two",
    }
    assert all(entry.status is CaseStatus.APPROVED for entry in after.cases)
    assert any(
        event.action == "deprecate" and event.decision is ReviewDecision.REJECT
        for event in after.review_log
    )

    # deprecate 必须逐项显式 approve
    third_draft = _case_draft(
        context,
        [
            _draft_case("create_order_min_quantity", body={"quantity": 1}),
            _draft_case("create_order_quantity_two", body={"quantity": 3}),
        ],
    )
    # 先删掉一个，再提交触发 deprecate
    third_draft.cases = [third_draft.cases[1]]
    third_path = tmp_path / "cases.draft.json"
    third_path.write_text(
        json.dumps(third_draft.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    third = _import(project, context, third_path, third_draft, after, store_path)
    deprecate_key = "POST /orders::create_order_min_quantity"
    assert {item.proposal_key: item.action for item in third.diff.items}[deprecate_key] == "deprecate"
    with pytest.raises(ProposalError, match="未审核"):
        approve_proposal(
            proposal_path=third.path,
            project=project,
            case_base=store_path,
            reviewer="alice",
        )
    _review(
        third.path,
        {
            "POST /orders::create_order_quantity_two": "approve",
            deprecate_key: "approve",
        },
    )
    approve_proposal(
        proposal_path=third.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    final = load_case_set(store_path)
    statuses = {entry.case_key: entry.status for entry in final.cases}
    assert statuses["create_order_min_quantity"] is CaseStatus.DEPRECATED


def test_empty_draft_cannot_clear_store(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context, draft_path, store_path = _prepare_imported(project, tmp_path)
    store = empty_case_set(project, context=context)
    draft = load_draft(draft_path)
    first = _import(project, context, draft_path, draft, store, store_path)
    _review(first.path, {"POST /orders::create_order_min_quantity": "approve"})
    approve_proposal(
        proposal_path=first.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    loaded = load_case_set(store_path)

    empty = _case_draft(context, [])
    empty_path = tmp_path / "empty.json"
    empty_path.write_text(json.dumps(empty.model_dump(mode="json")), encoding="utf-8")
    validation = validate_draft(empty, context, project=project)
    with pytest.raises(ProposalError, match="拒绝导入"):
        build_proposal(
            project=project,
            draft_path=empty_path,
            draft=empty,
            context=context,
            validation=validation,
            store=loaded,
            store_path=store_path,
        )


def test_unchanged_import_is_idempotent_and_needs_no_review(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context, draft_path, store_path = _prepare_imported(project, tmp_path)
    store = empty_case_set(project, context=context)
    draft = load_draft(draft_path)
    first = _import(project, context, draft_path, draft, store, store_path)
    _review(first.path, {"POST /orders::create_order_min_quantity": "approve"})
    approve_proposal(
        proposal_path=first.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    loaded = load_case_set(store_path)

    same = load_draft(draft_path)
    result = _import(project, context, draft_path, same, loaded, store_path)
    assert result.path is None
    assert result.reused is False
    assert result.diff.counts()["unchanged"] == 1

    # 同一 proposal 重复 import 会复用同一个待审目录
    update = _case_draft(
        context,
        [_draft_case("create_order_min_quantity", body={"quantity": 4})],
    )
    update_path = tmp_path / "update.json"
    update_path.write_text(
        json.dumps(update.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    a = _import(project, context, update_path, update, loaded, store_path)
    b = _import(project, context, update_path, update, loaded, store_path)
    assert a.path == b.path
    assert b.reused is True
    assert len(list_pending_proposals(project)) == 1


def test_approve_rejects_stale_store_and_tampered_candidate(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context, draft_path, store_path = _prepare_imported(project, tmp_path)
    store = empty_case_set(project, context=context)
    draft = load_draft(draft_path)
    first = _import(project, context, draft_path, draft, store, store_path)
    _review(first.path, {"POST /orders::create_order_min_quantity": "approve"})
    approve_proposal(
        proposal_path=first.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    loaded = load_case_set(store_path)

    update = _case_draft(
        context,
        [_draft_case("create_order_min_quantity", body={"quantity": 5})],
    )
    update_path = tmp_path / "update.json"
    update_path.write_text(
        json.dumps(update.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    proposal = _import(project, context, update_path, update, loaded, store_path)
    _review(proposal.path, {"POST /orders::create_order_min_quantity": "approve"})

    # 直接改 proposal 的候选内容但保留旧 fingerprint → 写库前重算必须拒绝
    proposal_json = proposal.path / "proposal.json"
    data = json.loads(proposal_json.read_text(encoding="utf-8"))
    data["items"][0]["candidate"]["body"] = {"quantity": 999}
    proposal_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises((ProposalError, CaseStoreError)):
        approve_proposal(
            proposal_path=proposal.path,
            project=project,
            case_base=store_path,
            reviewer="alice",
        )
    # 失败不能留下部分写入
    assert load_case_set(store_path).cases[0].body == {"quantity": 1}

    # 恢复 proposal 后，store 已变化 → 乐观锁拒绝
    data["items"][0]["candidate"]["body"] = {"quantity": 5}
    proposal_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    loaded.approved_by = "someone-else"
    save_case_set(store_path, loaded)
    with pytest.raises(ProposalError, match="变化"):
        approve_proposal(
            proposal_path=proposal.path,
            project=project,
            case_base=store_path,
            reviewer="alice",
        )


def test_approve_rejects_mixed_scope_without_silent_deprecation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context, draft_path, store_path = _prepare_imported(project, tmp_path)
    store = empty_case_set(project, context=context)
    draft = load_draft(draft_path)
    first = _import(project, context, draft_path, draft, store, store_path)
    _review(first.path, {"POST /orders::create_order_min_quantity": "approve"})
    approve_proposal(
        proposal_path=first.path,
        project=project,
        case_base=store_path,
        reviewer="alice",
    )
    loaded = load_case_set(store_path)

    # 跨文档用例：只把 docs/api.md 放进 scope，不能自动废弃
    loaded.cases[0].requirement_refs.append(
        SourceRef(doc="docs/other.md", section="other", quote="x")
    )
    loaded.cases[0].fingerprint = case_fingerprint(
        loaded.cases[0], loaded.cases[0].case_key
    )
    loaded.cases[0].review.fingerprint = loaded.cases[0].fingerprint
    validate_case_set(loaded)

    update = _case_draft(
        context,
        [_draft_case("create_order_min_quantity", body={"quantity": 6})],
    )
    update_path = tmp_path / "update.json"
    update_path.write_text(
        json.dumps(update.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    proposal = _import(project, context, update_path, update, loaded, store_path)
    # 锚点仍在草案中，所以是 update，不是 deprecate；范围外保留逻辑由 classify 单测覆盖
    assert proposal.diff.counts()["update"] == 1
    assert proposal.diff.counts()["deprecate"] == 0
