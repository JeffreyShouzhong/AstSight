"""A5/A6 合并节点：一次 LLM 调用完成失败归因与有界修复。

确定性归因先跑；只有高/中置信度的 `case_error` / `mock_error` 才允许修复。
修复对象包括用例请求/断言/绑定，以及用例实际使用的 Mock 语义分支。
修复不等于“让失败变绿”：断言条数与推导强度不得下降，身份字段不可改，
修改后的草案和 Mock 语义都必须重新通过确定性校验。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import DataCatalog, MockSemantic, MockStub, RuleSet

from ..cases.context import find_profile
from ..cases.draft import CaseDraft
from ..cases.strength import derive_assert_strength
from ..cases.validate import validate_draft
from ..llm import LLMClient, LLMError
from ..mock.server import FORBIDDEN_RESPONSE_HEADERS
from .casegen import validate_case_bindings
from .diagnosis import (
    ACTION_BY_ROOT,
    diagnosis_payload,
    is_repairable,
    select_failures,
)
from .errors import AgentError
from .models import (
    CasePatch,
    MockBranchPatch,
    RepairPlan,
    SemanticAnalysis,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "a5_diagnose_repair.md"
MAX_REPAIRS = 20
_STRENGTH_RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}


@dataclass
class RepairOutcome:
    draft: CaseDraft
    mock_semantic: MockSemantic | None = None
    diagnoses: dict[str, dict[str, Any]] = field(default_factory=dict)
    eligible_case_ids: set[str] = field(default_factory=set)
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    mock_applied: list[dict[str, Any]] = field(default_factory=list)
    mock_rejected: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def case_changed(self) -> bool:
        return bool(self.applied)

    @property
    def mock_changed(self) -> bool:
        return bool(self.mock_applied)

    @property
    def changed(self) -> bool:
        return self.case_changed or self.mock_changed


def load_repair_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else _PROMPT_PATH
    if not prompt_path.is_file():
        raise AgentError("repair_prompt_missing", str(prompt_path))
    return prompt_path.read_text(encoding="utf-8")


def _profile_view(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "endpoint": profile.get("endpoint"),
        "method": profile.get("method"),
        "path": profile.get("path"),
        "request_fields": profile.get("request_fields") or [],
        "response_fields": profile.get("response_fields") or [],
        "success_codes": profile.get("success_codes") or [],
        "error_codes": profile.get("error_codes") or [],
        "downstream_deps": [
            {
                "dep_id": dep.get("dep_id"),
                "path": dep.get("path_key"),
                "method": dep.get("method"),
            }
            for dep in profile.get("downstream_deps") or []
        ],
    }


def _apply_case_patch(case: Any, patch: CasePatch) -> None:
    for name in patch.changed_fields():
        setattr(case, name, copy.deepcopy(getattr(patch, name)))


def _all_assertions(case: Any) -> list[Any]:
    values = list(getattr(case, "asserts", []) or [])
    for step in getattr(case, "steps", []) or []:
        values.extend(getattr(step, "asserts", []) or [])
    return values


def _assertion_weakening(case: Any, patch: CasePatch) -> str | None:
    """断言条数或推导强度下降即拒绝；避免“删断言式修复”。"""
    if "asserts" not in patch.changed_fields():
        return None
    before_values = _all_assertions(case)
    after_values = list(patch.asserts or [])
    for step in getattr(case, "steps", []) or []:
        after_values.extend(getattr(step, "asserts", []) or [])
    if len(after_values) < len(before_values):
        return "assertion_count_decreased"
    steps = max(1, len(getattr(case, "steps", []) or []))
    before = str(derive_assert_strength(before_values, steps=steps))
    after = str(derive_assert_strength(after_values, steps=steps))
    if _STRENGTH_RANK.get(after, 1) < _STRENGTH_RANK.get(before, 1):
        return f"assert_strength_decreased:{before}->{after}"
    return None


def _validate_after(
    draft: CaseDraft,
    *,
    context: dict[str, Any],
    analysis: SemanticAnalysis | None,
    project: str | Path,
    ruleset: RuleSet | None = None,
    data_catalog: DataCatalog | None = None,
    mock_semantic: MockSemantic | None = None,
) -> list[str]:
    validation = validate_draft(
        draft,
        context,
        project=project,
        ruleset=ruleset,
        data_catalog=data_catalog,
        mock_semantic=mock_semantic,
        require_rules=ruleset is not None,
    )
    errors = [item.message for item in validation.errors]
    if analysis is not None:
        binding_errors, _ = validate_case_bindings(draft, analysis, context)
        errors.extend(binding_errors)
    return errors


def _branch_snapshot(
    semantic: MockSemantic, dep_id: str, branch_name: str
) -> Any | None:
    dep = semantic.dep_index().get(dep_id)
    if dep is None:
        return None
    return next((item for item in dep.branches if item.branch == branch_name), None)


def _restore_branch(
    semantic: MockSemantic, dep_id: str, branch_name: str, snapshot: Any
) -> None:
    dep = semantic.dep_index().get(dep_id)
    if dep is None:
        return
    for index, item in enumerate(dep.branches):
        if item.branch == branch_name:
            dep.branches[index] = snapshot
            return


def _mock_patch_allowed(case: Any, patch: MockBranchPatch) -> bool:
    bindings = getattr(case, "mock_bindings", {}) or {}
    return str(bindings.get(patch.dep_id) or "") == patch.branch


def _apply_mock_patch(
    semantic: MockSemantic, patch: MockBranchPatch
) -> tuple[dict[str, Any] | None, str | None]:
    changed = patch.changed_fields()
    if not changed:
        return None, "no_change"
    dep = semantic.dep_index().get(patch.dep_id)
    if dep is None:
        return None, "mock_dep_unknown"
    branch = next((item for item in dep.branches if item.branch == patch.branch), None)
    if branch is None:
        return None, "mock_branch_unknown"
    if "headers" in changed:
        forbidden = sorted(
            key
            for key in (patch.headers or {})
            if key.lower() in FORBIDDEN_RESPONSE_HEADERS
        )
        if forbidden:
            return None, f"mock_header_forbidden:{','.join(forbidden)}"
    stub_data = branch.stub.model_dump()
    if "status" in changed:
        stub_data["status"] = patch.status
    if "headers" in changed:
        stub_data["headers"] = dict(patch.headers or {})
    if "body" in changed:
        stub_data["body"] = copy.deepcopy(patch.body)
    try:
        branch.stub = MockStub.model_validate(stub_data)
    except Exception as exc:
        return None, f"mock_stub_invalid:{str(exc)[:200]}"
    if "semantics" in changed:
        branch.semantics = str(patch.semantics)
    return {
        "dep_id": patch.dep_id,
        "branch": patch.branch,
        "fields": changed,
        "reason": patch.reason,
    }, None


def _parse_plan(payload: Any) -> RepairPlan:
    if isinstance(payload, list):
        payload = {"schema_version": 2, "diagnoses": payload}
    return RepairPlan.model_validate(payload)


def diagnose_and_repair(
    draft: CaseDraft,
    *,
    cases: list[Any],
    entries: list[dict[str, Any]],
    llm: LLMClient,
    context: dict[str, Any],
    analysis: SemanticAnalysis | None,
    project: str | Path,
    mock_semantic: MockSemantic | None = None,
    mock_summary: dict[str, Any] | None = None,
    changed_endpoints: set[str] | None = None,
    case_keys: dict[str, str] | None = None,
    max_failures: int = 20,
    max_repairs: int = MAX_REPAIRS,
    prompt_path: str | Path | None = None,
    ruleset: RuleSet | None = None,
    data_catalog: DataCatalog | None = None,
) -> RepairOutcome:
    """一次 LLM 调用：诊断疑难失败，并在护栏内直接给草案/Mock 打补丁。"""
    key_by_case_id = case_keys or {}
    outcome = RepairOutcome(
        draft=draft.model_copy(deep=True),
        mock_semantic=(
            mock_semantic.model_copy(deep=True) if mock_semantic is not None else None
        ),
    )
    cases_by_id = {str(getattr(case, "id", "")): case for case in cases}
    selected = [
        entry
        for entry in select_failures(entries, max_failures)
        if str(entry.get("case_id") or "") in cases_by_id
    ]
    if not selected:
        return outcome

    changed = changed_endpoints or set()
    packages: list[dict[str, Any]] = []
    for entry in selected:
        case_id = str(entry.get("case_id") or "")
        case = cases_by_id[case_id]
        package = diagnosis_payload(
            entry,
            case,
            mock_summary,
            changed,
            case_key=key_by_case_id.get(case_id),
            mock_semantic=outcome.mock_semantic,
        )
        profile, _ = find_profile(context, case.endpoint)
        package["profile"] = _profile_view(profile) if profile else {}
        packages.append(package)

    system = load_repair_prompt(prompt_path)
    user = json.dumps(
        {
            "failures": packages,
            "schema": RepairPlan.model_json_schema(),
            "note": (
                "确定性归因已先行。请对每个失败给出归因；"
                "只有在证据支持时才对同一个 case 附带 patch。"
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("repair_llm_failed", str(exc)) from exc
    try:
        plan = _parse_plan(payload)
    except Exception as exc:
        raise AgentError("repair_invalid", str(exc)[:2_000]) from exc

    requested = {str(entry.get("case_id") or "") for entry in selected}
    entry_by_id = {str(entry.get("case_id") or ""): entry for entry in selected}
    seen_cases: set[str] = set()
    seen_mock_pairs: set[tuple[str, str]] = set()
    for decision in plan.diagnoses:
        if decision.case_id not in requested:
            outcome.errors.append(f"diagnosis_unknown_case:{decision.case_id}")
            continue
        if decision.case_id in seen_cases:
            outcome.warnings.append(f"diagnosis_duplicate_case:{decision.case_id}")
            continue
        seen_cases.add(decision.case_id)

        expected_target = ACTION_BY_ROOT[decision.root_cause]
        if decision.action_target != expected_target:
            outcome.warnings.append(
                f"diagnosis_action_target_corrected:{decision.case_id}:"
                f"{decision.action_target}->{expected_target}"
            )
            decision = decision.model_copy(
                update={"action_target": expected_target}
            )
        entry = entry_by_id[decision.case_id]
        case = cases_by_id[decision.case_id]
        case_key = key_by_case_id.get(decision.case_id)
        target = next(
            (item for item in outcome.draft.cases if item.case_key == case_key),
            None,
        )
        record = decision.model_dump(mode="json")
        record["eligible_for_repair"] = is_repairable(
            decision, entry, target or case
        )
        outcome.diagnoses[decision.case_id] = record
        if record["eligible_for_repair"]:
            outcome.eligible_case_ids.add(decision.case_id)

        patch = decision.patch
        if patch is None:
            continue
        if not record["eligible_for_repair"]:
            outcome.rejected.append(
                {"case_key": patch.case_key, "reason": "case_not_eligible"}
            )
            continue
        if not case_key or patch.case_key != case_key:
            outcome.rejected.append(
                {"case_key": patch.case_key, "reason": "case_key_mismatch"}
            )
            continue
        if target is None:
            outcome.rejected.append(
                {"case_key": case_key, "reason": "case_unknown"}
            )
            continue
        fields = patch.changed_fields()
        if not fields and not patch.mock_branches:
            outcome.rejected.append({"case_key": case_key, "reason": "no_change"})
            continue
        if len(outcome.applied) >= max_repairs:
            outcome.rejected.append(
                {"case_key": case_key, "reason": "repair_limit_reached"}
            )
            continue

        before = {
            name: copy.deepcopy(getattr(target, name))
            for name in fields
        }
        weakness = _assertion_weakening(target, patch)
        if weakness is not None:
            outcome.rejected.append(
                {"case_key": case_key, "reason": weakness}
            )
            continue

        branch_snapshots: dict[tuple[str, str], Any] = {}
        applied_mock: list[dict[str, Any]] = []
        reject_reason: str | None = None
        _apply_case_patch(target, patch)
        for mock_patch in patch.mock_branches:
            pair = (mock_patch.dep_id, mock_patch.branch)
            if pair in seen_mock_pairs:
                reject_reason = f"mock_branch_duplicate:{pair[0]}/{pair[1]}"
                break
            if not _mock_patch_allowed(target, mock_patch):
                reject_reason = f"mock_branch_not_bound:{pair[0]}/{pair[1]}"
                break
            if outcome.mock_semantic is None:
                reject_reason = "mock_semantic_missing"
                break
            branch_snapshots.setdefault(
                pair,
                copy.deepcopy(
                    _branch_snapshot(
                        outcome.mock_semantic, mock_patch.dep_id, mock_patch.branch
                    )
                ),
            )
            result, error = _apply_mock_patch(outcome.mock_semantic, mock_patch)
            if error is not None:
                reject_reason = error
                break
            assert result is not None
            applied_mock.append(result)

        if reject_reason is None:
            errors = _validate_after(
                outcome.draft,
                context=context,
                analysis=analysis,
                project=project,
                ruleset=ruleset,
                data_catalog=data_catalog,
                mock_semantic=outcome.mock_semantic,
            )
            if errors:
                reject_reason = "validation_failed"
                for name, value in before.items():
                    setattr(target, name, value)
                if outcome.mock_semantic is not None:
                    for (dep_id, branch_name), snapshot in branch_snapshots.items():
                        if snapshot is not None:
                            _restore_branch(
                                outcome.mock_semantic, dep_id, branch_name, snapshot
                            )
                outcome.rejected.append(
                    {
                        "case_key": case_key,
                        "reason": reject_reason,
                        "errors": errors[:10],
                    }
                )
                continue

        if reject_reason is not None:
            for name, value in before.items():
                setattr(target, name, value)
            if outcome.mock_semantic is not None:
                for (dep_id, branch_name), snapshot in branch_snapshots.items():
                    if snapshot is not None:
                        _restore_branch(
                            outcome.mock_semantic, dep_id, branch_name, snapshot
                        )
            outcome.rejected.append(
                {"case_key": case_key, "reason": reject_reason}
            )
            if reject_reason.startswith("mock_"):
                outcome.mock_rejected.append(
                    {"case_key": case_key, "reason": reject_reason}
                )
            continue

        if fields:
            outcome.applied.append(
                {
                    "case_key": case_key,
                    "fields": fields,
                    "reason": patch.reason,
                    "before": before,
                    "after": {
                        name: copy.deepcopy(getattr(target, name))
                        for name in fields
                    },
                }
            )
        for item in applied_mock:
            seen_mock_pairs.add((item["dep_id"], item["branch"]))
            item["case_key"] = case_key
            outcome.mock_applied.append(item)

    missing = requested - seen_cases
    for case_id in sorted(missing):
        outcome.warnings.append(f"diagnosis_missing:{case_id}")
    return outcome


__all__ = [
    "MAX_REPAIRS",
    "RepairOutcome",
    "diagnose_and_repair",
    "load_repair_prompt",
]
