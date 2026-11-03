"""规则间确定性冲突检测。

只比较结构化的 `RuleConstraint`，不尝试理解自由文本。检测结果是
`RuleConflict(origin="framework")`，只记录、不改变规则状态；冲突会进入
A4 输入与用例审核文件，由用例审核统一确认。
语义冲突（例如两句自然语言互相矛盾但字段/值不同）不在这里猜。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Iterable

from contracts import ConflictKind, Rule, RuleConflict, RuleConstraint, RuleOperator

_TOPIC_MAX = 200


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _value_key(value: Any) -> str:
    text = _norm(value)
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text.lower()
    return f"{number:.15g}"


def _num(value: Any) -> float | None:
    try:
        return float(_norm(value))
    except (TypeError, ValueError):
        return None


def _topic(field: str, condition: str) -> str:
    topic = f"constraint:{field}"
    if condition:
        topic += f"@{condition}"
    if len(topic) <= _TOPIC_MAX:
        return topic
    digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:8]
    return f"{topic[:_TOPIC_MAX - 9]}:{digest}"


def _values(constraint: RuleConstraint) -> list[str]:
    return [_value_key(value) for value in constraint.values]


def _value_set(constraint: RuleConstraint) -> set[str]:
    return set(_values(constraint))


def _first_value(constraint: RuleConstraint) -> str | None:
    values = _values(constraint)
    return values[0] if values else None


def _range(constraint: RuleConstraint) -> tuple[float | None, float | None] | None:
    values = [_num(value) for value in constraint.values]
    if constraint.operator is RuleOperator.MIN:
        return (values[0], None) if values and values[0] is not None else None
    if constraint.operator is RuleOperator.MAX:
        return (None, values[0]) if values and values[0] is not None else None
    if constraint.operator is RuleOperator.BETWEEN:
        numbers = [value for value in values if value is not None]
        if len(numbers) < 2:
            return None
        return (min(numbers[0], numbers[1]), max(numbers[0], numbers[1]))
    return None


def _in_range(value: float, bounds: tuple[float | None, float | None]) -> bool:
    low, high = bounds
    return (low is None or value >= low) and (high is None or value <= high)


def _overlap(
    first: tuple[float | None, float | None],
    second: tuple[float | None, float | None],
) -> bool:
    low = max(
        value for value in (first[0], second[0]) if value is not None
    ) if any(value is not None for value in (first[0], second[0])) else None
    high_candidates = [value for value in (first[1], second[1]) if value is not None]
    high = min(high_candidates) if high_candidates else None
    return low is None or high is None or low <= high


def _constraint_conflict(
    first: RuleConstraint,
    second: RuleConstraint,
) -> str | None:
    if (
        first.error_code
        and second.error_code
        and _value_key(first.error_code) != _value_key(second.error_code)
    ):
        return (
            f"同一字段/条件下错误码不一致: "
            f"{first.error_code!r} vs {second.error_code!r}"
        )

    op = first.operator
    other = second.operator
    if op is RuleOperator.OTHER or other is RuleOperator.OTHER:
        return None

    first_values = _value_set(first)
    second_values = _value_set(second)

    if op is RuleOperator.EQ:
        value = _first_value(first)
        if value is None:
            return None
        if other is RuleOperator.EQ and value != _first_value(second):
            return f"eq {first.values[0]!r} vs {second.values[0]!r}"
        if other is RuleOperator.NE and value in second_values:
            return f"eq {first.values[0]!r} 被 NE 排除"
        if other is RuleOperator.IN and value not in second_values:
            return f"eq {first.values[0]!r} 不在 IN 集合中"
        if other is RuleOperator.NOT_IN and value in second_values:
            return f"eq {first.values[0]!r} 被 NOT_IN 排除"
        bounds = _range(second)
        number = _num(value)
        if bounds is not None and number is not None and not _in_range(number, bounds):
            return f"eq {first.values[0]!r} 不在范围 {second.values!r} 内"
        return None

    if op is RuleOperator.IN:
        if other is RuleOperator.IN:
            if first_values.isdisjoint(second_values):
                return f"IN 集合不相交: {first.values!r} vs {second.values!r}"
            if first.exhaustive and first_values != second_values:
                extra = sorted(second_values - first_values)
                if extra:
                    return f"IN 完整集合之外出现值: {extra!r}"
            if second.exhaustive and first_values != second_values:
                extra = sorted(first_values - second_values)
                if extra:
                    return f"IN 完整集合之外出现值: {extra!r}"
            return None
        if other is RuleOperator.NOT_IN:
            if first_values and first_values <= second_values:
                return f"IN 集合全部被 NOT_IN 排除: {first.values!r}"
            return None
        if other in {
            RuleOperator.MIN,
            RuleOperator.MAX,
            RuleOperator.BETWEEN,
        }:
            bounds = _range(second)
            if bounds is None:
                return None
            numbers = [_num(value) for value in first_values]
            numeric = [number for number in numbers if number is not None]
            if numeric and not any(_in_range(number, bounds) for number in numeric):
                return f"IN 集合没有值落在范围 {second.values!r} 内"
        return None

    if op is RuleOperator.MIN:
        bounds = _range(second)
        if bounds is None:
            return None
        if other is RuleOperator.MAX:
            if bounds[1] is not None and _num(first.values[0]) is not None:
                if float(_num(first.values[0])) > bounds[1]:
                    return f"min > max: {first.values[0]!r} > {second.values[0]!r}"
        elif other is RuleOperator.BETWEEN and bounds[1] is not None:
            number = _num(first.values[0])
            if number is not None and number > bounds[1]:
                return f"min 大于 BETWEEN 上界: {first.values[0]!r}"
        return None

    if op is RuleOperator.MAX:
        bounds = _range(second)
        if bounds is None:
            return None
        if other is RuleOperator.MIN:
            number = _num(second.values[0])
            if number is not None and _num(first.values[0]) is not None:
                if float(_num(first.values[0])) < number:
                    return f"max < min: {first.values[0]!r} < {second.values[0]!r}"
        elif other is RuleOperator.BETWEEN and bounds[0] is not None:
            number = _num(first.values[0])
            if number is not None and number < bounds[0]:
                return f"max 小于 BETWEEN 下界: {first.values[0]!r}"
        return None

    if op is RuleOperator.BETWEEN:
        bounds = _range(second)
        if bounds is None:
            return None
        own = _range(first)
        if own is None:
            return None
        if other in {
            RuleOperator.MIN,
            RuleOperator.MAX,
            RuleOperator.BETWEEN,
        } and not _overlap(own, bounds):
            return f"范围不相交: {first.values!r} vs {second.values!r}"
        return None

    if op is RuleOperator.REQUIRED:
        if other is RuleOperator.FORBIDDEN:
            return "同一字段同时要求 REQUIRED 与 FORBIDDEN"
        return None

    if op is RuleOperator.NE:
        value = _first_value(first)
        if value is not None and other is RuleOperator.IN:
            if second.exhaustive and second_values == {value}:
                return f"NE {first.values[0]!r} 与完整 IN 集合矛盾"
        return None

    return None


def detect_constraint_conflicts(rules: Iterable[Rule]) -> list[RuleConflict]:
    """按 `(field, condition)` 分组比较结构化约束。"""
    groups: dict[tuple[str, str], list[tuple[Rule, RuleConstraint]]] = {}
    for rule in rules:
        for constraint in rule.constraints:
            field = _norm(constraint.field).lower()
            if not field:
                continue
            key = (field, _norm(constraint.condition).lower())
            groups.setdefault(key, []).append((rule, constraint))

    conflicts: list[RuleConflict] = []
    for (field, condition), facts in sorted(groups.items()):
        keys: set[str] = set()
        docs: set[str] = set()
        reasons: list[str] = []
        for (left_rule, left), (right_rule, right) in combinations(facts, 2):
            if left_rule.rule_key == right_rule.rule_key:
                continue
            reason = (
                _constraint_conflict(left, right)
                or _constraint_conflict(right, left)
            )
            if not reason:
                continue
            keys.update({left_rule.rule_key, right_rule.rule_key})
            docs.update({left_rule.source.doc, right_rule.source.doc})
            if reason not in reasons:
                reasons.append(reason)
        if not keys:
            continue
        conflicts.append(
            RuleConflict(
                topic=_topic(field, condition),
                kind=ConflictKind.VALUE,
                rule_keys=sorted(keys),
                docs=sorted(docs),
                origin="framework",
                reason="; ".join(reasons)[:2_000],
            )
        )
    return conflicts


__all__ = ["detect_constraint_conflicts"]
