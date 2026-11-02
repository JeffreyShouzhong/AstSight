"""规则库 diff：new / updated / unchanged / deprecated。

`rule_key` 已包含归一化文本，因此文本改写表现为 new + deprecated；
`updated` 覆盖 type/fields/authority/priority/source.kind 等仍同 key 的内容变化。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from contracts import ContractRule, Rule, RuleSet


@dataclass
class RuleChange:
    action: str
    rule_key: str
    rule_id: str
    kind: str
    text: str = ""
    previous_fingerprint: str = ""
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rule_key": self.rule_key,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "text": self.text,
            "previous_fingerprint": self.previous_fingerprint,
            "fingerprint": self.fingerprint,
        }


@dataclass
class RuleDiff:
    changes: list[RuleChange] = field(default_factory=list)
    drift: list[RuleChange] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for item in self.changes if item.action == action)

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": {
                "new": self.count("new"),
                "updated": self.count("updated"),
                "deprecated": self.count("deprecated"),
                "unchanged": self.count("unchanged"),
            },
            "changes": [item.as_dict() for item in self.changes],
            "drift": [item.as_dict() for item in self.drift],
        }


def _fingerprint(rule: Rule | ContractRule) -> str:
    """BR 用框架在导入时写入的 fingerprint；CR 无该字段，按内容即时计算。"""
    if isinstance(rule, Rule):
        return rule.fingerprint
    raw = json.dumps(
        rule.content_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def diff_rules(
    previous: RuleSet | None,
    rules: Iterable[Rule],
    contract_rules: Iterable[ContractRule],
) -> RuleDiff:
    old_index = previous.rule_index() if previous else {}
    new_rules = list(rules)
    new_contract = list(contract_rules)
    seen: set[str] = set()
    result = RuleDiff()
    for rule in [*new_rules, *new_contract]:
        kind = "BR" if isinstance(rule, Rule) else "CR"
        key = rule.rule_key
        seen.add(key)
        old = old_index.get(key)
        if old is None:
            change = RuleChange(
                action="new",
                rule_key=key,
                rule_id=rule.rule_id,
                kind=kind,
                text=getattr(rule, "text", "") or getattr(rule, "constraint", ""),
                fingerprint=_fingerprint(rule),
            )
            result.changes.append(change)
            continue
        old_fp = _fingerprint(old)
        new_fp = _fingerprint(rule)
        action = "unchanged" if old_fp and old_fp == new_fp else "updated"
        change = RuleChange(
            action=action,
            rule_key=key,
            rule_id=rule.rule_id,
            kind=kind,
            text=getattr(rule, "text", "") or getattr(rule, "constraint", ""),
            previous_fingerprint=old_fp,
            fingerprint=new_fp,
        )
        result.changes.append(change)
        if action == "updated":
            result.drift.append(change)
    for key, old in old_index.items():
        if key in seen:
            continue
        result.changes.append(
            RuleChange(
                action="deprecated",
                rule_key=key,
                rule_id=getattr(old, "rule_id", ""),
                kind="BR" if isinstance(old, Rule) else "CR",
                text=getattr(old, "text", "") or getattr(old, "constraint", ""),
                previous_fingerprint=_fingerprint(old),
            )
        )
    return result


def orphaned_rule_keys(rule_keys: Iterable[str], ruleset: RuleSet | None) -> list[str]:
    if ruleset is None:
        return list(dict.fromkeys(rule_keys))
    known = set(ruleset.rule_index())
    return [key for key in dict.fromkeys(rule_keys) if key not in known]


def drift_summary(diff: RuleDiff) -> dict[str, Any]:
    return {
        "updated": [item.as_dict() for item in diff.drift],
        "deprecated": [item.as_dict() for item in diff.changes if item.action == "deprecated"],
    }


__all__ = ["RuleChange", "RuleDiff", "diff_rules", "drift_summary", "orphaned_rule_keys"]
