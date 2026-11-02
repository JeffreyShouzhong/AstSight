"""C3.5.1 下游 Mock 语义构建与校验。

契约层（dep/route/默认 success）确定性生成；业务分支只从规则映射，
或由适配层补 candidates，再逐项校验。无依据的分支不生成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from contracts import (
    Evidence,
    MockBranch,
    MockBranchSource,
    MockDep,
    MockSemantic,
    MockSemanticGap,
    MockSemanticQuestion,
    MockStub,
    MockStrategy,
    Rule,
    RuleSet,
    RuleStatus,
    Transport,
)

_FAILURE_HINTS = (
    "失败",
    "错误",
    "异常",
    "超时",
    "拒绝",
    "不可用",
    "降级",
    "重试",
    "非法",
    "invalid",
    "error",
    "timeout",
    "fail",
    "reject",
    "unavailable",
)
_ERROR_RULES = {"error_handling", "state_transition", "data_consistency", "validation"}
_STATUS_RE = re.compile(r"(?<!\d)([45]\d{2})(?!\d)")
_MAX_BRANCHES = 64


class MockSemanticError(ValueError):
    """Mock 语义不合法。"""


@dataclass
class MockSemanticApplyResult:
    semantic: MockSemantic
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _dep_tokens(dep: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("service_name", "target", "route_host", "path_key", "config_key"):
        value = str(dep.get(key) or "")
        if not value:
            continue
        if key in {"target"}:
            try:
                value = urlsplit(value).hostname or value
            except ValueError:
                pass
        tokens.update(
            part.lower()
            for part in re.split(r"[^A-Za-z0-9一-鿿]+", value)
            if len(part) >= 3
        )
    return tokens


def _rule_matches_dep(rule: Rule, dep: dict[str, Any], *, only_dep: bool) -> bool:
    text = rule.text.lower()
    failed = any(hint in text for hint in _FAILURE_HINTS) or bool(
        _STATUS_RE.search(text)
    )
    if not failed:
        return False
    if any(token and token in text for token in _dep_tokens(dep)):
        return True
    if only_dep and any(
        word in text
        for word in ("下游", "依赖", "第三方", "降级", "超时", "服务")
    ):
        return True
    return False


def _default_branch() -> MockBranch:
    return MockBranch(
        branch="success",
        semantics="下游正常返回",
        stub=MockStub(status=200, body={}),
        evidence=Evidence.INFERRED,
        source=MockBranchSource(kind="contract", rule_keys=[]),
    )


def _rule_branch(rule: Rule) -> MockBranch:
    branch = f"rule_{rule.rule_id}"
    match = _STATUS_RE.search(rule.text)
    status = int(match.group(1)) if match else 500
    return MockBranch(
        branch=branch,
        semantics=rule.text,
        stub=MockStub(
            status=status,
            body={"error": "downstream_error", "rule_id": rule.rule_id},
        ),
        evidence=Evidence.INFERRED,
        source=MockBranchSource(kind="rule", rule_keys=[rule.rule_key]),
    )


def build_mock_semantic(
    context: dict[str, Any],
    ruleset: RuleSet | None = None,
) -> MockSemantic:
    deps: dict[str, MockDep] = {}
    gaps: list[MockSemanticGap] = []
    profiles = context.get("profiles") or []
    active_rules = [
        rule
        for rule in (ruleset.rules if ruleset is not None else [])
        if rule.status is RuleStatus.ACTIVE and rule.type.value in _ERROR_RULES
    ]
    for profile in profiles:
        profile_deps = profile.get("downstream_deps") or []
        only_dep = len(profile_deps) == 1
        for raw in profile_deps:
            dep_id = str(raw.get("dep_id") or "")
            if not dep_id:
                continue
            dep = deps.get(dep_id)
            if dep is None:
                method = str(raw.get("method") or "").upper()
                path_key = str(raw.get("path_key") or "")
                dep = MockDep(
                    dep_id=dep_id,
                    transport=Transport.HTTP,
                    endpoint=f"{method} {path_key}".strip(),
                    route_key=path_key,
                    strategy=MockStrategy.URL_OVERRIDE,
                    property=(
                        str((raw.get("config_keys") or [""])[0])
                        if raw.get("config_keys")
                        else None
                    ),
                    branches=[_default_branch()],
                )
                deps[dep_id] = dep
            existing = {branch.branch for branch in dep.branches}
            for rule in active_rules:
                if not _rule_matches_dep(rule, raw, only_dep=only_dep):
                    continue
                branch = _rule_branch(rule)
                if branch.branch in existing:
                    continue
                if len(dep.branches) >= _MAX_BRANCHES:
                    gaps.append(
                        MockSemanticGap(
                            dep_id=dep_id,
                            reason=f"branch_limit:{_MAX_BRANCHES}",
                        )
                    )
                    break
                dep.branches.append(branch)
                existing.add(branch.branch)
        if not profile_deps:
            gaps.append(
                MockSemanticGap(
                    dep_id="",
                    reason=f"downstream_unmapped:{profile.get('endpoint', '')}",
                )
            )
    return MockSemantic(
        schema_version=1,
        context_id=str(context.get("context_id") or ""),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        deps=sorted(deps.values(), key=lambda item: item.dep_id),
        gaps=gaps,
        questions=[],
    )


def _base_dep_index(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for profile in context.get("profiles") or []:
        for dep in profile.get("downstream_deps") or []:
            dep_id = str(dep.get("dep_id") or "")
            if dep_id:
                index.setdefault(dep_id, dep)
    return index


def apply_mock_semantic_candidates(
    semantic: MockSemantic,
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    ruleset: RuleSet | None = None,
) -> MockSemanticApplyResult:
    """合并适配层候选分支；只允许既有 dep，分支必须有规则依据。"""
    errors: list[str] = []
    warnings: list[str] = []
    deps = semantic.dep_index()
    known_context = _base_dep_index(context)
    known_rules = set(ruleset.rule_index()) if ruleset is not None else set()
    raw_deps = payload.get("deps") if isinstance(payload, dict) else None
    if not isinstance(raw_deps, list):
        errors.append("deps_not_list")
        return MockSemanticApplyResult(semantic, errors, warnings)

    for item in raw_deps:
        if not isinstance(item, dict):
            errors.append("dep_not_object")
            continue
        dep_id = str(item.get("dep_id") or "")
        dep = deps.get(dep_id)
        if dep is None:
            errors.append(f"dep_unknown:{dep_id}")
            continue
        if dep_id not in known_context:
            errors.append(f"dep_not_in_profile:{dep_id}")
            continue
        raw_branches = item.get("branches") or []
        if not isinstance(raw_branches, list):
            errors.append(f"branches_not_list:{dep_id}")
            continue
        existing = {branch.branch for branch in dep.branches}
        for raw in raw_branches:
            if not isinstance(raw, dict):
                errors.append(f"branch_not_object:{dep_id}")
                continue
            try:
                candidate = MockBranch.model_validate(raw)
            except Exception as exc:
                errors.append(f"branch_invalid:{dep_id}:{exc}")
                continue
            if candidate.branch in existing:
                if candidate.branch == "success" and candidate.source.rule_keys:
                    current = next(
                        branch
                        for branch in dep.branches
                        if branch.branch == candidate.branch
                    )
                    current.stub = candidate.stub
                    current.semantics = candidate.semantics
                    current.source = candidate.source
                    warnings.append(
                        f"branch_stub_overridden:{dep_id}:{candidate.branch}"
                    )
                    continue
                errors.append(f"branch_duplicate:{dep_id}:{candidate.branch}")
                continue
            if not candidate.source.rule_keys:
                errors.append(
                    f"branch_ungrounded:{dep_id}:{candidate.branch}"
                )
                continue
            unknown = [
                key for key in candidate.source.rule_keys if key not in known_rules
            ]
            if unknown:
                errors.append(
                    f"branch_rule_unknown:{dep_id}:{candidate.branch}:{unknown}"
                )
                continue
            if candidate.evidence is Evidence.VERIFIED:
                warnings.append(
                    f"branch_downgraded:{dep_id}:{candidate.branch}"
                )
                candidate.evidence = Evidence.INFERRED
            dep.branches.append(candidate)
            existing.add(candidate.branch)
        for gap in item.get("gaps") or []:
            if isinstance(gap, dict):
                semantic.gaps.append(
                    MockSemanticGap(
                        dep_id=dep_id,
                        reason=str(gap.get("reason") or "unknown")[:1_000],
                    )
                )
    for raw in payload.get("questions") or []:
        if isinstance(raw, dict):
            try:
                semantic.questions.append(MockSemanticQuestion.model_validate(raw))
            except Exception as exc:
                errors.append(f"question_invalid:{exc}")
    return MockSemanticApplyResult(semantic, sorted(set(errors)), warnings)


def validate_mock_semantic(
    semantic: MockSemantic,
    context: dict[str, Any],
    *,
    ruleset: RuleSet | None = None,
) -> list[str]:
    errors: list[str] = []
    known_deps = _base_dep_index(context)
    known_rules = set(ruleset.rule_index()) if ruleset is not None else set()
    seen_deps: set[str] = set()
    for dep in semantic.deps:
        if dep.dep_id in seen_deps:
            errors.append(f"dep_duplicate:{dep.dep_id}")
        seen_deps.add(dep.dep_id)
        if known_deps and dep.dep_id not in known_deps:
            errors.append(f"dep_not_in_profile:{dep.dep_id}")
        if dep.transport is not Transport.HTTP:
            errors.append(f"transport_unsupported:{dep.dep_id}")
        if not dep.branches:
            errors.append(f"dep_no_branch:{dep.dep_id}")
        names: set[str] = set()
        for branch in dep.branches:
            if branch.branch in names:
                errors.append(f"branch_duplicate:{dep.dep_id}:{branch.branch}")
            names.add(branch.branch)
            if not (200 <= branch.stub.status <= 599):
                errors.append(
                    f"branch_status_invalid:{dep.dep_id}:{branch.branch}"
                )
            if branch.source.kind == "rule" and not branch.source.rule_keys:
                errors.append(
                    f"branch_rule_missing:{dep.dep_id}:{branch.branch}"
                )
            for key in branch.source.rule_keys:
                if known_rules and key not in known_rules:
                    errors.append(
                        f"branch_rule_unknown:{dep.dep_id}:{branch.branch}:{key}"
                    )
    return errors


__all__ = [
    "MockSemanticApplyResult",
    "MockSemanticError",
    "apply_mock_semantic_candidates",
    "build_mock_semantic",
    "validate_mock_semantic",
]
