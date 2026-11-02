"""草案 diff、审核文件与批准落库。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import (
    CaseEntry,
    CaseReview,
    CaseSet,
    CaseStatus,
    DataCatalog,
    RequirementDoc,
    ReviewDecision,
    ReviewEvent,
    RuleSet,
)

from ..persistence import load_json, save_json_atomic, sha256_json, sha256_text
from .context import context_documents
from .context import counted_section_names
from .draft import CaseDraft, draft_fingerprint, load_draft, proposal_key
from .store import (
    EMPTY_STORE_HASH,
    allocate_case_id,
    empty_case_set,
    find_case,
    load_case_set,
    revision_matches,
    save_case_set,
    store_hash,
    validate_case_set,
)
from .validate import ValidationResult, validate_draft

PROPOSAL_SCHEMA_VERSION = 1
PROPOSALS_DIR = Path(".astsight") / "cases" / "proposals"


class ProposalError(RuntimeError):
    """proposal/approve 状态不一致。"""


@dataclass
class DiffItem:
    proposal_key: str
    action: str
    case_key: str
    endpoint: str
    fingerprint: str
    store_case_id: str | None = None
    current_version: int | None = None
    proposed_version: int | None = None
    title: str = ""
    candidate: dict[str, Any] | None = None
    existing: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_key": self.proposal_key,
            "action": self.action,
            "case_key": self.case_key,
            "endpoint": self.endpoint,
            "fingerprint": self.fingerprint,
            "store_case_id": self.store_case_id,
            "current_version": self.current_version,
            "proposed_version": self.proposed_version,
            "title": self.title,
            "candidate": self.candidate,
            "existing": self.existing,
            "warnings": self.warnings,
        }


@dataclass
class DiffResult:
    items: list[DiffItem] = field(default_factory=list)
    unchanged: list[dict[str, Any]] = field(default_factory=list)
    kept_out_of_scope: list[dict[str, Any]] = field(default_factory=list)

    @property
    def actionable(self) -> list[DiffItem]:
        return [
            item for item in self.items
            if item.action in {"new", "update", "revive", "deprecate"}
        ]

    def counts(self) -> dict[str, int]:
        counts = {
            "new": 0,
            "update": 0,
            "revive": 0,
            "deprecate": 0,
            "unchanged": len(self.unchanged),
            "kept_out_of_scope": len(self.kept_out_of_scope),
        }
        for item in self.items:
            counts[item.action] = counts.get(item.action, 0) + 1
        return counts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _case_warnings(validation: ValidationResult, endpoint: str, case_key: str) -> list[dict]:
    return [
        item.model_dump()
        for item in validation.warnings
        if item.case_key == case_key and item.endpoint in (None, "", endpoint)
    ]


def classify(
    validation: ValidationResult,
    store: CaseSet,
    *,
    scope_docs: list[str],
) -> DiffResult:
    """把已校验草案按当前 store 分类为 six-way diff。"""
    result = DiffResult()
    scope = set(scope_docs)
    matched: set[tuple[str, str]] = set()

    for resolved in validation.resolved:
        case = resolved.case
        case_key = resolved.case_key
        endpoint = case.endpoint
        matched.add((endpoint, case_key))
        existing = find_case(store, endpoint=endpoint, case_key=case_key)
        candidate = case.model_dump(mode="json")
        warnings = _case_warnings(validation, endpoint, case_key)
        if existing is None:
            result.items.append(
                DiffItem(
                    proposal_key=proposal_key(endpoint, case_key),
                    action="new",
                    case_key=case_key,
                    endpoint=endpoint,
                    fingerprint=resolved.fingerprint,
                    proposed_version=1,
                    title=case.title or case_key,
                    candidate=candidate,
                    warnings=warnings,
                )
            )
            continue
        if existing.status is CaseStatus.DEPRECATED:
            result.items.append(
                DiffItem(
                    proposal_key=proposal_key(endpoint, case_key),
                    action="revive",
                    case_key=case_key,
                    endpoint=endpoint,
                    fingerprint=resolved.fingerprint,
                    store_case_id=existing.id,
                    current_version=existing.version,
                    proposed_version=existing.version + 1,
                    title=case.title or case_key,
                    candidate=candidate,
                    existing=existing.model_dump(mode="json"),
                    warnings=warnings,
                )
            )
            continue
        if existing.fingerprint == resolved.fingerprint:
            result.unchanged.append(
                {
                    "case_key": case_key,
                    "endpoint": endpoint,
                    "case_id": existing.id,
                    "version": existing.version,
                    "fingerprint": existing.fingerprint,
                }
            )
            continue
        result.items.append(
            DiffItem(
                proposal_key=proposal_key(endpoint, case_key),
                action="update",
                case_key=case_key,
                endpoint=endpoint,
                fingerprint=resolved.fingerprint,
                store_case_id=existing.id,
                current_version=existing.version,
                proposed_version=existing.version + 1,
                title=case.title or case_key,
                candidate=candidate,
                existing=existing.model_dump(mode="json"),
                warnings=warnings,
            )
        )

    for entry in store.cases:
        anchor = (entry.endpoint, entry.case_key)
        if anchor in matched:
            continue
        if entry.status is CaseStatus.DEPRECATED:
            continue
        docs = {ref.doc for ref in entry.requirement_refs if ref.doc}
        if not docs:
            result.kept_out_of_scope.append(
                {
                    "case_key": entry.case_key,
                    "endpoint": entry.endpoint,
                    "case_id": entry.id,
                    "reason": "no_requirement_refs",
                }
            )
            continue
        if not docs <= scope:
            result.kept_out_of_scope.append(
                {
                    "case_key": entry.case_key,
                    "endpoint": entry.endpoint,
                    "case_id": entry.id,
                    "reason": "doc_not_in_scope",
                    "docs": sorted(docs),
                }
            )
            continue
        result.items.append(
            DiffItem(
                proposal_key=proposal_key(entry.endpoint, entry.case_key),
                action="deprecate",
                case_key=entry.case_key,
                endpoint=entry.endpoint,
                fingerprint=entry.fingerprint,
                store_case_id=entry.id,
                current_version=entry.version,
                proposed_version=entry.version,
                title=entry.title or entry.case_key,
                existing=entry.model_dump(mode="json"),
                warnings=[],
            )
        )

    result.items.sort(key=lambda item: (item.endpoint, item.case_key, item.action))
    return result


def _requirements_snapshot(
    context: dict[str, Any], scope_docs: list[str]
) -> dict[str, Any]:
    scope = set(scope_docs)
    documents = [
        doc for doc in context.get("documents") or [] if str(doc["doc"]) in scope
    ]
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "context_id": context.get("context_id"),
        "openapi": context.get("openapi") or {},
        "documents": documents,
    }


def _review_json(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal["proposal_id"],
        "instructions": (
            "将每个 action 项的 decision 改为 approve 或 reject；pending 会阻止批准。"
            "不要修改 action/fingerprint；修改用例请改 draft 后重新 import。"
        ),
        "items": [
            {
                "proposal_key": item["proposal_key"],
                "action": item["action"],
                "case_key": item["case_key"],
                "endpoint": item["endpoint"],
                "store_case_id": item["store_case_id"],
                "fingerprint": item["fingerprint"],
                "title": item["title"],
                "decision": "pending",
                "reviewer": None,
                "note": None,
            }
            for item in proposal["items"]
        ],
    }


def _request_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "path_params": candidate.get("path_params") or {},
        "query": candidate.get("query") or {},
        "headers": candidate.get("headers") or {},
        "body": candidate.get("body"),
        "mock_bindings": candidate.get("mock_bindings") or {},
    }


def _render_review_md(
    proposal: dict[str, Any],
    draft: CaseDraft,
    warnings: list[dict[str, Any]],
    ruleset: RuleSet | None = None,
) -> str:
    counts = proposal["counts"]
    rules_snapshot = proposal.get("base_ruleset") or {}
    lines = [
        f"# 用例审核提案 `{proposal['proposal_id']}`",
        "",
        f"- context_id: `{proposal['context_id']}`",
        f"- 基线 store: v{proposal['base_store_version']} "
        f"`{proposal['base_store_hash'][:16]}…`",
        f"- 草案指纹: `{proposal['draft_fingerprint'][:16]}…`",
    ]
    if rules_snapshot:
        lines.append(
            f"- 规则库: `{rules_snapshot.get('id')}` v{rules_snapshot.get('version')} "
            f"`{str(rules_snapshot.get('hash') or '')[:16]}…`"
        )
    lines.extend(
        [
        f"- 新增 {counts.get('new', 0)} / 更新 {counts.get('update', 0)} / "
        f"恢复 {counts.get('revive', 0)} / 废弃 {counts.get('deprecate', 0)} / "
        f"不变 {counts.get('unchanged', 0)} / 范围外保留 "
        f"{counts.get('kept_out_of_scope', 0)}",
        "",
        "> 审核：编辑同目录的 `cases.review.json`，把 decision 从 `pending` 改为 "
        "`approve` 或 `reject`。`deprecate` 必须逐项显式 approve，不会批量自动废弃。",
        "> 修改用例内容请改 `cases.draft.json` 后重新 `cases import`；本审核文件只承载决定。",
        "",
        "## 待审项",
        "",
        "| # | action | endpoint | case_key | title | fingerprint |",
        "|---|---|---|---|---|---|",
        ]
    )
    for index, item in enumerate(proposal["items"], 1):
        lines.append(
            f"| {index} | {item['action']} | `{item['endpoint']}` | "
            f"`{item['case_key']}` | {item['title']} | "
            f"`{item['fingerprint'][:12]}…` |"
        )
    lines.extend(["", "## 明细", ""])
    for index, item in enumerate(proposal["items"], 1):
        lines.extend(
            [
                f"### {index}. {item['action']} · `{item['case_key']}`",
                "",
                f"- endpoint: `{item['endpoint']}`",
                f"- store: `{item['store_case_id']}` "
                f"v{item['current_version']} → v{item['proposed_version']}",
                f"- fingerprint: `{item['fingerprint']}`",
            ]
        )
        candidate = item.get("candidate") or {}
        existing = item.get("existing") or {}
        if candidate and candidate.get("rule_refs"):
            rule_index = ruleset.rule_index() if ruleset is not None else {}
            lines.extend(["", "规则依据："])
            for ref in candidate.get("rule_refs") or []:
                key = str(ref.get("rule_key") or "")
                rule = rule_index.get(key)
                if rule is None:
                    lines.append(f"- `{ref.get('rule_id') or key}`（规则库中未找到）")
                    continue
                text = (
                    rule.constraint
                    if hasattr(rule, "constraint")
                    else getattr(rule, "text", "")
                )
                source = getattr(rule, "source", None)
                section = getattr(source, "section", "") if source else ""
                kind = getattr(getattr(source, "kind", None), "value", "")
                lines.append(
                    f"- `{rule.rule_id}` {rule.authority.value} "
                    f"[{kind}] {text}  \n  来源: `{section}`"
                )
        if candidate and candidate.get("level"):
            lines.append(
                f"- level: `{candidate.get('level')}` / transport "
                f"`{candidate.get('transport')}` / coverage "
                f"`{candidate.get('coverage_role')}`"
            )
        if candidate and candidate.get("preconditions"):
            lines.extend(
                [
                    "",
                    "前置数据状态：",
                    "```json",
                    json.dumps(
                        candidate.get("preconditions"), ensure_ascii=False, indent=2
                    ),
                    "```",
                ]
            )
        if candidate:
            lines.extend(
                [
                    f"- category: `{candidate.get('category')}` / priority "
                    f"`{candidate.get('priority')}` / strength "
                    f"`{candidate.get('assert_strength')}`",
                    "",
                    "请求：",
                    "```json",
                    json.dumps(
                        _request_summary(candidate), ensure_ascii=False, indent=2
                    ),
                    "```",
                    "",
                    "断言：",
                    "```json",
                    json.dumps(
                        candidate.get("asserts") or [], ensure_ascii=False, indent=2
                    ),
                    "```",
                ]
            )
            if candidate.get("rationale"):
                lines.extend(["", f"设计依据：{candidate['rationale']}"])
            if candidate.get("note"):
                lines.extend(["", f"备注：{candidate['note']}"])
            refs = candidate.get("requirement_refs") or []
            if refs:
                lines.extend(["", "需求引用："])
                for ref in refs:
                    lines.extend(
                        [
                            f"- `{ref.get('doc')}#{ref.get('section')}`",
                            f"  > {ref.get('quote')}",
                        ]
                    )
        if existing:
            lines.extend(
                [
                    "",
                    "当前已批准版本：",
                    "```json",
                    json.dumps(
                        _request_summary(existing), ensure_ascii=False, indent=2
                    ),
                    "```",
                ]
            )
        for warning in item.get("warnings") or []:
            lines.append(f"- ⚠ `{warning.get('code')}`: {warning.get('message')}")
        lines.append("")

    if warnings:
        lines.extend(["## 全局警告", ""])
        for warning in warnings:
            lines.append(
                f"- `{warning.get('code')}`: {warning.get('message')}"
            )
        lines.append("")
    if draft.uncovered_requirements:
        lines.extend(["## 未覆盖需求", ""])
        for item in draft.uncovered_requirements:
            lines.append(
                f"- `{item.doc}#{item.section}` — `{item.reason}`"
            )
        lines.append("")
    return "\n".join(lines)


def _ruleset_snapshot(ruleset: RuleSet | None) -> dict[str, Any]:
    if ruleset is None:
        return {}
    return {
        "id": ruleset.ruleset_id,
        "version": ruleset.version,
        "hash": sha256_json(ruleset.model_dump(mode="json")),
    }


def _data_catalog_snapshot(catalog: DataCatalog | None) -> dict[str, Any]:
    if catalog is None:
        return {}
    return {
        "id": catalog.catalog_id,
        "hash": sha256_json(catalog.model_dump(mode="json")),
        "tables": len(catalog.tables),
        "states": len(catalog.states),
    }


def _proposal_signature(
    context_id: str,
    base_store_version: int,
    base_store_hash: str,
    draft_hash: str,
    case_base: str,
    ruleset_hash: str = "",
    catalog_hash: str = "",
) -> str:
    return sha256_json(
        {
            "context_id": context_id,
            "base_store_version": base_store_version,
            "base_store_hash": base_store_hash,
            "draft_fingerprint": draft_hash,
            "case_base": case_base,
            "ruleset_hash": ruleset_hash,
            "catalog_hash": catalog_hash,
        }
    )


def _proposal_root(project: str | Path) -> Path:
    return Path(project).resolve() / PROPOSALS_DIR


def _find_pending_by_signature(project: str | Path, signature: str) -> Path | None:
    root = _proposal_root(project)
    if not root.is_dir():
        return None
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        proposal_path = candidate / "proposal.json"
        if not proposal_path.is_file() or (candidate / "approval.json").exists():
            continue
        try:
            data = load_json(proposal_path)
        except Exception:
            continue
        if data.get("signature") == signature and data.get("status") == "pending":
            return candidate.resolve()
    return None


@dataclass
class ProposalResult:
    path: Path | None
    proposal: dict[str, Any] | None
    diff: DiffResult
    reused: bool = False

    @property
    def has_actionable(self) -> bool:
        return bool(self.diff.actionable)


def build_proposal(
    *,
    project: str | Path,
    draft_path: str | Path,
    draft: CaseDraft,
    context: dict[str, Any],
    validation: ValidationResult,
    store: CaseSet,
    store_path: str | Path,
    ruleset: RuleSet | None = None,
    data_catalog: DataCatalog | None = None,
) -> ProposalResult:
    if validation.has_errors:
        raise ProposalError("草案校验失败，不能生成 proposal")
    if not draft.cases and (store.cases or store.history):
        raise ProposalError("草案为空但用例库非空，拒绝导入以防误清空")

    diff = classify(validation, store, scope_docs=validation.scope_docs)
    if not diff.actionable:
        return ProposalResult(path=None, proposal=None, diff=diff)

    rules_snapshot = _ruleset_snapshot(ruleset)
    catalog_snapshot = _data_catalog_snapshot(data_catalog)
    store_path_resolved = Path(store_path).resolve()
    store_exists = store_path_resolved.is_file()
    base_hash = store_hash(store) if store_exists else EMPTY_STORE_HASH
    signature = _proposal_signature(
        str(context.get("context_id") or ""),
        store.store_version,
        base_hash,
        draft_fingerprint(draft),
        store_path_resolved.as_posix(),
        str(rules_snapshot.get("hash") or ""),
        str(catalog_snapshot.get("hash") or ""),
    )
    existing = _find_pending_by_signature(project, signature)
    if existing is not None:
        return ProposalResult(
            path=existing,
            proposal=load_json(existing / "proposal.json"),
            diff=diff,
            reused=True,
        )

    proposal_id = "prop-" + signature[:16]
    proposal_dir = _proposal_root(project) / proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)

    warnings = [item.model_dump() for item in validation.warnings]
    context_docs = set(context_documents(context))
    for doc in sorted(set(store.doc_versions) - context_docs):
        warnings.append(
            {
                "severity": "warning",
                "code": "requirement.document_not_in_scope",
                "message": f"store 中的文档本次未传入，原用例保留: {doc}",
            }
        )

    proposal = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "signature": signature,
        "status": "pending",
        "created_at": _now(),
        "applied_at": None,
        "project": Path(project).resolve().name,
        "context_id": context.get("context_id"),
        "context_dir": "",
        "draft_path": str(Path(draft_path).resolve()),
        "draft_snapshot": "cases.draft.json",
        "draft_fingerprint": draft_fingerprint(draft),
        "draft_sha256": sha256_text(Path(draft_path).read_text(encoding="utf-8-sig")),
        "base_store_version": store.store_version,
        "base_store_hash": base_hash,
        "base_store_exists": store_exists,
        "case_base": store_path_resolved.as_posix(),
        "base_ruleset": rules_snapshot,
        "base_data_catalog": catalog_snapshot,
        "scope_docs": validation.scope_docs,
        "openapi": context.get("openapi") or {},
        "counts": diff.counts(),
        "items": [item.as_dict() for item in diff.items],
        "unchanged": diff.unchanged,
        "kept_out_of_scope": diff.kept_out_of_scope,
        "unaccounted_requirements": validation.unaccounted,
        "warnings": warnings,
        "uncovered_requirements": [
            item.model_dump(mode="json")
            for item in draft.uncovered_requirements
        ],
    }

    # 原始 draft / context 快照：approve 不信任审核文件，也不依赖原 context 目录仍在。
    (proposal_dir / "cases.draft.json").write_bytes(Path(draft_path).read_bytes())
    save_json_atomic(proposal_dir / "context_snapshot.json", context)
    save_json_atomic(
        proposal_dir / "requirements_snapshot.json",
        _requirements_snapshot(context, validation.scope_docs),
    )
    save_json_atomic(proposal_dir / "validation.json", validation.as_dict())
    if ruleset is not None:
        save_json_atomic(
            proposal_dir / "rules_snapshot.json", ruleset.model_dump(mode="json")
        )
    if data_catalog is not None:
        save_json_atomic(
            proposal_dir / "data_catalog_snapshot.json",
            data_catalog.model_dump(mode="json"),
        )
    save_json_atomic(proposal_dir / "proposal.json", proposal)
    save_json_atomic(proposal_dir / "cases.review.json", _review_json(proposal))
    (proposal_dir / "cases.review.md").write_text(
        _render_review_md(proposal, draft, warnings, ruleset),
        encoding="utf-8",
        newline="\n",
    )
    return ProposalResult(path=proposal_dir.resolve(), proposal=proposal, diff=diff)


def load_proposal(path: str | Path) -> tuple[Path, dict[str, Any]]:
    raw = Path(path)
    if raw.is_dir():
        raw = raw / "proposal.json"
    if not raw.is_file():
        raise ProposalError(f"proposal 不存在: {raw}")
    proposal = load_json(raw)
    if not isinstance(proposal, dict) or not proposal.get("proposal_id"):
        raise ProposalError(f"proposal 格式错误: {raw}")
    return raw.parent.resolve(), proposal


def list_pending_proposals(project: str | Path) -> list[dict[str, Any]]:
    root = _proposal_root(project)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        if (candidate / "approval.json").exists():
            continue
        proposal_path = candidate / "proposal.json"
        if not proposal_path.is_file():
            continue
        try:
            data = load_json(proposal_path)
        except Exception as exc:
            out.append(
                {
                    "path": str(candidate.resolve()),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if data.get("status") == "pending":
            out.append(
                {
                    "path": str(candidate.resolve()),
                    "proposal_id": data.get("proposal_id"),
                    "created_at": data.get("created_at"),
                    "counts": data.get("counts") or {},
                    "base_store_version": data.get("base_store_version"),
                }
            )
    return out


def _load_review(proposal_dir: Path, proposal_id: str) -> dict[str, dict[str, Any]]:
    path = proposal_dir / "cases.review.json"
    if not path.is_file():
        raise ProposalError(f"审核文件不存在: {path}")
    data = load_json(path)
    if data.get("proposal_id") != proposal_id:
        raise ProposalError("审核文件 proposal_id 与 proposal 不一致")
    items: dict[str, dict[str, Any]] = {}
    for item in data.get("items") or []:
        key = str(item.get("proposal_key") or "")
        if not key:
            raise ProposalError("审核文件存在缺少 proposal_key 的项")
        if key in items:
            raise ProposalError(f"审核文件重复项: {key}")
        items[key] = item
    return items


def _load_store_for_approval(
    store_path: Path, project: str | Path, context: dict[str, Any]
) -> CaseSet:
    store = load_case_set(store_path)
    if store is None:
        store = empty_case_set(project, context=context)
    return store


def _find_entry(store: CaseSet, case_id: str) -> tuple[int, CaseEntry]:
    for index, entry in enumerate(store.cases):
        if entry.id == case_id:
            return index, entry
    raise ProposalError(f"store 中找不到用例: {case_id}")


def _entry_from_candidate(
    *,
    candidate: dict[str, Any],
    case_key: str,
    case_id: str,
    version: int,
    fingerprint: str,
    reviewer: str,
    note: str | None,
    created_at: str,
    now: str,
) -> CaseEntry:
    payload = dict(candidate)
    payload.pop("id", None)
    payload.pop("version", None)
    payload["id"] = case_id
    payload["case_key"] = case_key
    payload["status"] = CaseStatus.APPROVED
    payload["version"] = version
    payload["fingerprint"] = fingerprint
    payload["fingerprint_version"] = 2
    payload["review"] = CaseReview(
        decision=ReviewDecision.APPROVE,
        by=reviewer,
        at=now,
        note=note,
        fingerprint=fingerprint,
    )
    payload["created_at"] = created_at
    payload["updated_at"] = now
    return CaseEntry.model_validate(payload)


def _update_requirements(
    store: CaseSet,
    *,
    context: dict[str, Any],
    scope_docs: list[str],
    uncovered: list[dict[str, Any]],
) -> None:
    scope = set(scope_docs)
    snapshot_docs = {
        str(doc["doc"]): doc for doc in context.get("documents") or []
    }
    existing = {requirement.doc: requirement for requirement in store.requirements}
    for doc in scope:
        raw = snapshot_docs.get(doc)
        if raw is None:
            continue
        store.doc_versions[doc] = str(raw["version"])
        sections = counted_section_names(raw.get("sections") or [])
        existing[doc] = RequirementDoc(
            doc=doc, version=str(raw["version"]), sections=sections
        )
    store.requirements = sorted(existing.values(), key=lambda item: item.doc)

    approved_covered: set[str] = set()
    for entry in store.cases:
        if entry.status is not CaseStatus.APPROVED:
            continue
        for ref in entry.requirement_refs:
            if ref.doc and ref.section:
                approved_covered.add(f"{ref.doc}::{ref.section}")
    for key in list(store.uncovered_reasons):
        if key in approved_covered:
            del store.uncovered_reasons[key]
    for item in uncovered:
        doc = str(item.get("doc") or "")
        section = str(item.get("section") or "")
        reason = str(item.get("reason") or "uncovered")
        if doc in scope:
            store.uncovered_reasons[f"{doc}::{section}"] = reason


def approve_proposal(
    *,
    proposal_path: str | Path,
    project: str | Path,
    case_base: str | Path,
    reviewer: str,
) -> dict[str, Any]:
    proposal_dir, proposal = load_proposal(proposal_path)
    if not reviewer or not reviewer.strip():
        raise ProposalError("必须显式提供 --reviewer")
    approval_path = proposal_dir / "approval.json"
    if approval_path.exists():
        return load_json(approval_path)

    proposal_id = str(proposal["proposal_id"])
    context_path = proposal_dir / "context_snapshot.json"
    draft_path = proposal_dir / str(proposal.get("draft_snapshot") or "cases.draft.json")
    if not context_path.is_file():
        raise ProposalError(f"proposal 缺少 context 快照: {context_path}")
    if not draft_path.is_file():
        raise ProposalError(f"proposal 缺少 draft 快照: {draft_path}")
    context = load_json(context_path)
    draft = load_draft(draft_path)
    if draft_fingerprint(draft) != proposal.get("draft_fingerprint"):
        raise ProposalError("proposal 的 draft 快照指纹不匹配，请重新 import")

    ruleset: RuleSet | None = None
    rules_snapshot = proposal.get("base_ruleset") or {}
    rules_path = proposal_dir / "rules_snapshot.json"
    if rules_snapshot:
        if not rules_path.is_file():
            raise ProposalError("proposal 缺少 rules_snapshot.json，请重新 import")
        ruleset = RuleSet.model_validate(load_json(rules_path))
        expected_hash = sha256_json(ruleset.model_dump(mode="json"))
        if expected_hash != str(rules_snapshot.get("hash") or ""):
            raise ProposalError("rules_snapshot.json 与 proposal 的规则快照不一致")
    data_catalog: DataCatalog | None = None
    catalog_snapshot = proposal.get("base_data_catalog") or {}
    catalog_path = proposal_dir / "data_catalog_snapshot.json"
    if catalog_snapshot:
        if not catalog_path.is_file():
            raise ProposalError(
                "proposal 缺少 data_catalog_snapshot.json，请重新 import"
            )
        data_catalog = DataCatalog.model_validate(load_json(catalog_path))
        expected_hash = sha256_json(data_catalog.model_dump(mode="json"))
        if expected_hash != str(catalog_snapshot.get("hash") or ""):
            raise ProposalError(
                "data_catalog_snapshot.json 与 proposal 的目录快照不一致"
            )

    store_path = Path(case_base)
    if not store_path.is_absolute():
        store_path = Path(project).resolve() / store_path
    store_path = store_path.resolve()
    proposed_case_base = str(proposal.get("case_base") or "")
    if proposed_case_base and Path(proposed_case_base).resolve() != store_path:
        raise ProposalError(
            f"proposal 针对的 case store 是 {proposed_case_base}，"
            f"不是 {store_path}"
        )
    store_exists = store_path.is_file()
    expected_exists = bool(proposal.get("base_store_exists", True))
    if store_exists != expected_exists:
        raise ProposalError(
            "case store 存在状态与 proposal 基线不一致；请重新 cases import"
        )
    if store_exists:
        store = load_case_set(store_path)
        if store is None or not revision_matches(
            store,
            int(proposal["base_store_version"]),
            str(proposal["base_store_hash"]),
        ):
            raise ProposalError(
                "case store 已在 proposal 创建后变化；请重新 cases import"
            )
    else:
        if (
            int(proposal["base_store_version"]) != 0
            or str(proposal["base_store_hash"]) != EMPTY_STORE_HASH
        ):
            raise ProposalError("空 case store 基线与 proposal 不一致")
        store = empty_case_set(project, context=context)

    validation = validate_draft(
        draft,
        context,
        project=project,
        check_doc_hashes=False,
        ruleset=ruleset,
        data_catalog=data_catalog,
        require_rules=ruleset is not None,
    )
    if validation.has_errors:
        raise ProposalError(
            "批准前重新校验失败: "
            + "; ".join(issue.message for issue in validation.errors[:5])
        )
    current_diff = classify(
        validation, store, scope_docs=list(proposal.get("scope_docs") or [])
    )
    proposed = {
        item["proposal_key"]: item for item in proposal.get("items") or []
    }
    current = {item.proposal_key: item for item in current_diff.items}
    for key, item in proposed.items():
        other = current.get(key)
        if other is None:
            raise ProposalError(f"批准前 re-diff 缺失项: {key}")
        if other.action != item["action"] or other.fingerprint != item["fingerprint"]:
            raise ProposalError(
                f"批准前 re-diff 与 proposal 不一致: {key} "
                f"({item['action']}/{item['fingerprint'][:8]} → "
                f"{other.action}/{other.fingerprint[:8]})"
            )
    for key in set(current) - set(proposed):
        raise ProposalError(f"批准前出现 proposal 未包含的变更: {key}")

    review = _load_review(proposal_dir, proposal_id)
    for key in set(review) - set(proposed):
        raise ProposalError(f"审核文件包含未知项: {key}")
    decisions: dict[str, dict[str, Any]] = {}
    for key in proposed:
        item = review.get(key)
        if item is None:
            raise ProposalError(f"审核文件缺少项: {key}")
        try:
            decision = ReviewDecision(str(item.get("decision")))
        except ValueError as exc:
            raise ProposalError(f"未知审核决定: {item.get('decision')!r}") from exc
        if decision is ReviewDecision.PENDING:
            raise ProposalError(f"仍有未审核项: {key}")
        decisions[key] = {
            "decision": decision,
            "note": item.get("note"),
        }

    now = _now()
    events: list[ReviewEvent] = []
    applied: list[dict[str, Any]] = []

    for key, item in proposed.items():
        decision = decisions[key]["decision"]
        note = decisions[key]["note"]
        action = item["action"]
        case_id = item.get("store_case_id")
        fingerprint = str(item["fingerprint"])
        case_key = str(item["case_key"])

        if decision is ReviewDecision.REJECT:
            events.append(
                ReviewEvent(
                    proposal_id=proposal_id,
                    case_id=case_id,
                    case_key=case_key,
                    action=action,
                    decision=ReviewDecision.REJECT,
                    reviewer=reviewer,
                    at=now,
                    note=note,
                    fingerprint=fingerprint,
                )
            )
            applied.append(
                {
                    "proposal_key": key,
                    "case_id": case_id,
                    "action": action,
                    "decision": "reject",
                }
            )
            continue

        if action == "new":
            new_id = allocate_case_id(store)
            entry = _entry_from_candidate(
                candidate=item["candidate"],
                case_key=case_key,
                case_id=new_id,
                version=1,
                fingerprint=fingerprint,
                reviewer=reviewer,
                note=note,
                created_at=now,
                now=now,
            )
            store.cases.append(entry)
            applied_id = new_id
        elif action in {"update", "revive"}:
            if not case_id:
                raise ProposalError(f"{key} 缺少 store_case_id")
            index, existing = _find_entry(store, case_id)
            if action == "update" and existing.status is not CaseStatus.APPROVED:
                raise ProposalError(f"{case_id} 不是 approved，不能 update")
            if action == "revive" and existing.status is not CaseStatus.DEPRECATED:
                raise ProposalError(f"{case_id} 不是 deprecated，不能 revive")
            old = existing.model_copy(deep=True)
            old.status = CaseStatus.SUPERSEDED
            old.updated_at = now
            store.history.append(old)
            entry = _entry_from_candidate(
                candidate=item["candidate"],
                case_key=case_key,
                case_id=existing.id,
                version=existing.version + 1,
                fingerprint=fingerprint,
                reviewer=reviewer,
                note=note,
                created_at=existing.created_at,
                now=now,
            )
            store.cases[index] = entry
            applied_id = entry.id
        elif action == "deprecate":
            if not case_id:
                raise ProposalError(f"{key} 缺少 store_case_id")
            index, existing = _find_entry(store, case_id)
            if existing.status is not CaseStatus.APPROVED:
                raise ProposalError(f"{case_id} 不是 approved，不能 deprecate")
            deprecate = existing.model_copy(deep=True)
            deprecate.status = CaseStatus.DEPRECATED
            deprecate.updated_at = now
            deprecate.review = CaseReview(
                decision=ReviewDecision.APPROVE,
                by=reviewer,
                at=now,
                note=note,
                fingerprint=existing.fingerprint,
            )
            store.cases[index] = deprecate
            applied_id = deprecate.id
        else:
            raise ProposalError(f"未知 action: {action}")

        events.append(
            ReviewEvent(
                proposal_id=proposal_id,
                case_id=applied_id,
                case_key=case_key,
                action=action,
                decision=ReviewDecision.APPROVE,
                reviewer=reviewer,
                at=now,
                note=note,
                fingerprint=fingerprint,
            )
        )
        applied.append(
            {
                "proposal_key": key,
                "case_id": applied_id,
                "action": action,
                "decision": "approve",
            }
        )

    # 写库前再读一次磁盘，缩小版本检查与替换之间的窗口。
    disk = load_case_set(store_path) if store_path.is_file() else None
    disk_exists = disk is not None
    disk_version = disk.store_version if disk else 0
    disk_hash = store_hash(disk) if disk else EMPTY_STORE_HASH
    if (
        disk_exists != expected_exists
        or disk_version != int(proposal["base_store_version"])
        or disk_hash != str(proposal["base_store_hash"])
    ):
        raise ProposalError("case store 在批准过程中被其他进程修改，请重新 import")

    if store.store_version == 0 and not store.cases and not store.history:
        store.generated_at = now
    store.store_version = int(proposal["base_store_version"]) + 1
    store.updated_at = now
    store.approved_at = now
    store.approved_by = reviewer
    store.openapi = dict(proposal.get("openapi") or store.openapi)
    if rules_snapshot:
        store.ruleset = dict(rules_snapshot)
    store.review_log.extend(events)
    _update_requirements(
        store,
        context=context,
        scope_docs=list(proposal.get("scope_docs") or []),
        uncovered=list(proposal.get("uncovered_requirements") or []),
    )
    validate_case_set(store)
    save_case_set(store_path, store)

    approval = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "applied_at": now,
        "reviewer": reviewer,
        "base_store_version": proposal["base_store_version"],
        "base_store_hash": proposal["base_store_hash"],
        "store_version": store.store_version,
        "store_hash": store_hash(store),
        "decisions": applied,
    }
    save_json_atomic(approval_path, approval)
    proposal["status"] = "applied"
    proposal["applied_at"] = now
    proposal["store_version"] = store.store_version
    save_json_atomic(proposal_dir / "proposal.json", proposal)
    return approval


__all__ = [
    "PROPOSALS_DIR",
    "PROPOSAL_SCHEMA_VERSION",
    "DiffItem",
    "DiffResult",
    "ProposalError",
    "ProposalResult",
    "approve_proposal",
    "build_proposal",
    "classify",
    "list_pending_proposals",
    "load_proposal",
]
