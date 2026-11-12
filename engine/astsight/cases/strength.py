"""按断言内容推导 assert_strength，避免把 L1 冒充成业务判定。

口径（保守，可由人工在设计阶段上调）：
- L1：状态码 / 字段存在 / 结构（body_schema）；
- L2：确定值（body_field / header / db_row 的非存在性断言）；
- L3：引用上一步响应的关系式（`$ref`）；
- L4：同时引用两个及以上不同步骤的跨响应不变量。
"""
from __future__ import annotations

from typing import Any, Iterable

from contracts import AssertStrength

_RANK = {
    AssertStrength.L1: 1,
    AssertStrength.L2: 2,
    AssertStrength.L3: 3,
    AssertStrength.L4: 4,
}
_VALUE_OPS = {"eq", "ne", "in", "gte", "lte", "contains", "count_eq"}


def _ref_steps(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            out.add(value["$ref"].partition(".")[0])
            return out
        for item in value.values():
            out |= _ref_steps(item)
    elif isinstance(value, list):
        for item in value:
            out |= _ref_steps(item)
    return out


def derive_assert_strength(
    assertions: Iterable[Any],
    *,
    steps: int = 1,
) -> AssertStrength:
    literal_value = False
    ref_steps: set[str] = set()
    for assertion in assertions:
        kind = str(getattr(assertion, "kind", ""))
        if kind == "body_schema":
            continue
        # 只有 body_field / db_row 的 expect 与 db_row 的 where 支持 $ref；
        # header 明确不支持，status_code 也不按引用执行。
        if kind in {"body_field", "db_row"}:
            ref_steps |= _ref_steps(getattr(assertion, "expect", None))
        if kind == "db_row":
            ref_steps |= _ref_steps(getattr(assertion, "where", None) or {})
        if kind in {"body_field", "header", "db_row"} and str(
            getattr(assertion, "op", "")
        ) in _VALUE_OPS:
            literal_value = True
    if len(ref_steps) >= 2 and steps >= 2:
        return AssertStrength.L4
    if ref_steps:
        return AssertStrength.L3
    if literal_value:
        return AssertStrength.L2
    return AssertStrength.L1


def max_strength(left: AssertStrength, right: AssertStrength) -> AssertStrength:
    return left if _RANK[left] >= _RANK[right] else right


__all__ = ["derive_assert_strength", "max_strength"]
