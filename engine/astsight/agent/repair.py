"""A6 修复建议：对未批准的草案做有界、可审计的修改。

修复不等于“让失败变绿”：只有确定性命中 case_error、且 A5 给出高/中置信度时才允许；
修改后的草案重新走完整 validate_draft，再生成新的待审 proposal。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import DataCatalog, MockSemantic, RuleSet

from ..cases.context import find_profile
from ..cases.draft import CaseDraft
from ..cases.validate import validate_draft
from ..llm import LLMClient, LLMError
from .casegen import validate_case_bindings
from .errors import AgentError
from .models import CasePatch, RepairPlan, SemanticAnalysis

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_AST_SIGHT_ROOT = _ENGINE_ROOT.parent
_PROMPT_PATH = _AST_SIGHT_ROOT / "prompts" / "a6_case_repair.md"
MAX_REPAIRS = 20


@dataclass
class RepairOutcome:
    draft: CaseDraft
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


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


def _apply_patch(case: Any, patch: CasePatch) -> None:
    for name in patch.changed_fields():
        setattr(case, name, getattr(patch, name))


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


def propose_repairs(
    draft: CaseDraft,
    *,
    failures: list[dict[str, Any]],
    diagnoses: dict[str, dict[str, Any]],
    context: dict[str, Any],
    analysis: SemanticAnalysis | None,
    llm: LLMClient,
    project: str | Path,
    case_keys: dict[str, str] | None = None,
    max_repairs: int = MAX_REPAIRS,
    prompt_path: str | Path | None = None,
    ruleset: RuleSet | None = None,
    data_catalog: DataCatalog | None = None,
    mock_semantic: MockSemantic | None = None,
) -> RepairOutcome:
    """返回新草案；不写库、不批准、不执行。"""
    key_by_case_id = case_keys or {}
    outcome = RepairOutcome(draft=draft.model_copy(deep=True))
    cases_by_key = {case.case_key: case for case in outcome.draft.cases}
    packages: list[dict[str, Any]] = []
    eligible_keys: set[str] = set()
    for failure in failures:
        case_id = str(failure.get("case_id") or "")
        record = diagnoses.get(case_id)
        if not record or not record.get("eligible_for_repair"):
            continue
        case_key = key_by_case_id.get(case_id) or str(failure.get("case_key") or "")
        case = cases_by_key.get(case_key)
        if case is None:
            continue
        profile, _ = find_profile(context, case.endpoint)
        if profile is None:
            continue
        packages.append(
            {
                "case": case.model_dump(mode="json"),
                "execution": {
                    "message": str(failure.get("message") or "")[:8_000],
                    "status_code": failure.get("status_code"),
                    "deterministic": failure.get("diagnosis") or {},
                },
                "diagnosis": record,
                "profile": _profile_view(profile),
            }
        )
        eligible_keys.add(case_key)
        if len(packages) >= max_repairs:
            break

    if not packages:
        return outcome

    system = load_repair_prompt(prompt_path)
    user = json.dumps(
        {"failures": packages, "schema": RepairPlan.model_json_schema()},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        payload = llm.complete_json(system=system, user=user)
    except LLMError as exc:
        raise AgentError("repair_llm_failed", str(exc)) from exc
    try:
        plan = RepairPlan.model_validate(payload)
    except Exception as exc:
        raise AgentError("repair_invalid", str(exc)[:2_000]) from exc

    for patch in plan.repairs:
        if patch.case_key not in eligible_keys:
            outcome.rejected.append(
                {"case_key": patch.case_key, "reason": "case_not_eligible"}
            )
            continue
        case = cases_by_key.get(patch.case_key)
        if case is None:
            outcome.rejected.append(
                {"case_key": patch.case_key, "reason": "case_unknown"}
            )
            continue
        if not patch.changed_fields():
            outcome.rejected.append(
                {"case_key": patch.case_key, "reason": "no_change"}
            )
            continue
        before = {
            name: copy.deepcopy(getattr(case, name))
            for name in patch.changed_fields()
        }
        _apply_patch(case, patch)
        errors = _validate_after(
            outcome.draft,
            context=context,
            analysis=analysis,
            project=project,
            ruleset=ruleset,
            data_catalog=data_catalog,
            mock_semantic=mock_semantic,
        )
        if errors:
            for name, value in before.items():
                setattr(case, name, value)
            outcome.rejected.append(
                {"case_key": patch.case_key, "reason": "validation_failed",
                 "errors": errors[:10]}
            )
            continue
        outcome.applied.append(
            {
                "case_key": patch.case_key,
                "fields": patch.changed_fields(),
                "reason": patch.reason,
                "before": before,
                "after": {
                    name: copy.deepcopy(getattr(case, name))
                    for name in patch.changed_fields()
                },
            }
        )
    return outcome


__all__ = [
    "MAX_REPAIRS",
    "RepairOutcome",
    "load_repair_prompt",
    "propose_repairs",
]
