"""规则覆盖：BR / CR 分开统计，不把契约约束混进业务规则分母。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from contracts import CaseEntry, RuleSet, RuleStatus, TestCase


@dataclass
class RuleCoverageItem:
    rule_key: str
    rule_id: str
    kind: str
    status: str
    authority: str
    cases: list[str] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return bool(self.cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_key": self.rule_key,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "status": self.status,
            "authority": self.authority,
            "cases": list(self.cases),
            "covered": self.covered,
        }


@dataclass
class RuleCoverage:
    business: list[RuleCoverageItem] = field(default_factory=list)
    contract: list[RuleCoverageItem] = field(default_factory=list)
    unknown_rule_keys: dict[str, list[str]] = field(default_factory=dict)

    def _stats(self, items: list[RuleCoverageItem]) -> dict[str, Any]:
        active = [item for item in items if item.status == RuleStatus.ACTIVE.value]
        covered = [item for item in active if item.covered]
        return {
            "total": len(items),
            "active": len(active),
            "covered": len(covered),
            "uncovered": len(active) - len(covered),
            "deprecated": sum(
                1 for item in items if item.status == RuleStatus.DEPRECATED.value
            ),
            "disputed": sum(
                1 for item in items if item.status == RuleStatus.DISPUTED.value
            ),
            "ungrounded": sum(
                1 for item in items if item.status == RuleStatus.UNGROUNDED.value
            ),
            "ratio": round(len(covered) / len(active), 4) if active else 1.0,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "business": {
                "stats": self._stats(self.business),
                "rules": [item.as_dict() for item in self.business],
            },
            "contract": {
                "stats": self._stats(self.contract),
                "rules": [item.as_dict() for item in self.contract],
            },
            "unknown_rule_keys": {
                key: list(cases) for key, cases in self.unknown_rule_keys.items()
            },
        }


def _case_label(case: TestCase) -> str:
    return str(getattr(case, "case_id", None) or getattr(case, "case_key", "") or "")


def rule_coverage(
    ruleset: RuleSet | None,
    cases: Iterable[TestCase | CaseEntry],
) -> RuleCoverage:
    case_list = list(cases)
    refs: dict[str, list[str]] = {}
    for case in case_list:
        label = _case_label(case)
        for ref in getattr(case, "rule_refs", []) or []:
            refs.setdefault(ref.rule_key, [])
            if label and label not in refs[ref.rule_key]:
                refs[ref.rule_key].append(label)

    result = RuleCoverage()
    if ruleset is None:
        result.unknown_rule_keys = {
            key: labels for key, labels in refs.items()
        }
        return result

    known: set[str] = set()
    for rule in ruleset.rules:
        known.add(rule.rule_key)
        item = RuleCoverageItem(
            rule_key=rule.rule_key,
            rule_id=rule.rule_id,
            kind="BR",
            status=rule.status.value,
            authority=rule.authority.value,
            cases=list(refs.get(rule.rule_key, [])),
        )
        result.business.append(item)
    for rule in ruleset.contract_rules:
        known.add(rule.rule_key)
        item = RuleCoverageItem(
            rule_key=rule.rule_key,
            rule_id=rule.rule_id,
            kind="CR",
            status=rule.status.value,
            authority=rule.authority.value,
            cases=list(refs.get(rule.rule_key, [])),
        )
        result.contract.append(item)
    result.unknown_rule_keys = {
        key: labels for key, labels in refs.items() if key not in known
    }
    return result


__all__ = ["RuleCoverage", "RuleCoverageItem", "rule_coverage"]
