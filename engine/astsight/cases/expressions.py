"""断言期望值的受限表达式语法。

表达式只用于断言右侧取值，不进入请求值或规则表达式。支持：

- ``{"$field": "items"}``：当前响应内的字段；
- ``{"$ref": "create.id"}``：更早步骤响应内的字段；
- ``{"$op": "len", "args": [{"$field": "items"}]}``：受限算术/集合运算。

这里只做语法分类和校验，执行由生成的 ``conftest.py`` 完成。
"""
from __future__ import annotations

import re
from typing import Any, Iterator

RESERVED_KEYS = frozenset({"$ref", "$field", "$request", "$op"})
OP_ARITY: dict[str, tuple[int, int]] = {
    "len": (1, 1),
    "sum": (1, 1),
    "add": (2, 16),
    "sub": (2, 2),
    "mul": (2, 16),
    "div": (2, 2),
}
MAX_DEPTH = 16
MAX_NODES = 128
MAX_FIELD_PATH_CHARS = 256

_FIELD_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def dynamic_kind(value: Any) -> str | None:
    """返回动态节点类型；普通字面量返回 ``None``，保留键形状错误返回 invalid。"""
    if not isinstance(value, dict) or not any(key in value for key in RESERVED_KEYS):
        return None
    if set(value) == {"$ref"}:
        return "ref"
    if set(value) == {"$field"}:
        return "field"
    if set(value) == {"$request"}:
        return "request"
    if set(value) == {"$op", "args"}:
        return "op"
    return "invalid"


def is_step_ref(value: Any) -> bool:
    return dynamic_kind(value) == "ref"


def is_field_ref(value: Any) -> bool:
    return dynamic_kind(value) == "field"


def has_dynamic(value: Any) -> bool:
    """值中是否包含任一动态节点。"""
    if dynamic_kind(value) is not None:
        return True
    if isinstance(value, dict):
        return any(has_dynamic(item) for item in value.values())
    if isinstance(value, list):
        return any(has_dynamic(item) for item in value)
    return False


def has_relation_expression(value: Any) -> bool:
    """是否包含同响应字段或运算表达式。"""
    if dynamic_kind(value) in {"field", "request", "op"}:
        return True
    if isinstance(value, dict):
        return any(has_relation_expression(item) for item in value.values())
    if isinstance(value, list):
        return any(has_relation_expression(item) for item in value)
    return False


def field_paths(value: Any) -> list[str]:
    """收集表达式中的当前响应字段路径。"""
    out: list[str] = []
    if is_field_ref(value):
        if isinstance(value.get("$field"), str):
            out.append(value["$field"])
        return out
    if isinstance(value, dict):
        for item in value.values():
            out.extend(field_paths(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(field_paths(item))
    return out


def iter_dynamic_nodes(
    value: Any,
    *,
    location: str = "value",
    max_depth: int = MAX_DEPTH,
    max_nodes: int = MAX_NODES,
) -> Iterator[tuple[Any, str]]:
    """按深度优先顺序返回动态节点及其位置，并限制遍历规模。"""
    stack: list[tuple[Any, str, int]] = [(value, location, 0)]
    seen = 0
    while stack:
        node, where, depth = stack.pop()
        seen += 1
        if seen > max_nodes:
            raise ValueError(f"表达式节点超过上限 {max_nodes}")
        if depth > max_depth:
            raise ValueError(f"表达式嵌套超过上限 {max_depth}")
        kind = dynamic_kind(node)
        if kind is not None:
            yield node, where
            if kind == "op" and isinstance(node.get("args"), list):
                for index, item in reversed(list(enumerate(node["args"]))):
                    stack.append((item, f"{where}.args[{index}]", depth + 1))
            continue
        if isinstance(node, dict):
            for key, item in reversed(list(node.items())):
                stack.append((item, f"{where}.{key}", depth + 1))
        elif isinstance(node, list):
            for index, item in reversed(list(enumerate(node))):
                stack.append((item, f"{where}[{index}]", depth + 1))


def validate_dynamic(value: Any, *, allow_response_expr: bool) -> list[tuple[str, str]]:
    """校验动态节点语法；返回 ``(code, message)``，无错误时为空。"""
    errors: list[tuple[str, str]] = []
    try:
        nodes = list(iter_dynamic_nodes(value))
    except ValueError as exc:
        return [("expression.too_complex", str(exc))]

    for node, where in nodes:
        kind = dynamic_kind(node)
        if kind == "ref":
            # 步骤名和允许引用的步骤由 validate._check_step_refs 统一校验。
            continue
        if kind == "field":
            if not allow_response_expr:
                errors.append(
                    (
                        "expression.field_not_allowed",
                        f"{where} 的 $field 只能用于当前响应断言",
                    )
                )
                continue
            path = node.get("$field")
            if not isinstance(path, str) or not _valid_field_path(path):
                errors.append(
                    (
                        "expression.field_invalid",
                        f"{where} 的 $field 必须是点分隔字段路径",
                    )
                )
            continue
        if kind == "request":
            if not allow_response_expr:
                errors.append(
                    (
                        "expression.request_not_allowed",
                        f"{where} 的 $request 只能用于断言",
                    )
                )
                continue
            path = node.get("$request")
            if not isinstance(path, str) or not _valid_field_path(path):
                errors.append(
                    (
                        "expression.request_invalid",
                        f"{where} 的 $request 必须是点分隔字段路径",
                    )
                )
            continue
        if kind == "op":
            if not allow_response_expr:
                errors.append(
                    (
                        "expression.op_not_allowed",
                        f"{where} 的 $op 只能用于当前响应断言",
                    )
                )
                continue
            op = node.get("$op")
            args = node.get("args")
            if not isinstance(op, str) or op not in OP_ARITY:
                errors.append(
                    (
                        "expression.op_invalid",
                        f"{where} 不支持表达式运算 {op!r}",
                    )
                )
                continue
            if not isinstance(args, list):
                errors.append(
                    ("expression.args_invalid", f"{where} 的 args 必须是列表")
                )
                continue
            low, high = OP_ARITY[op]
            if not low <= len(args) <= high:
                expected = str(low) if low == high else f"{low}..{high}"
                errors.append(
                    (
                        "expression.args_invalid",
                        f"{where} 的 {op} 需要 {expected} 个参数，实际 {len(args)} 个",
                    )
                )
            continue
        errors.append(
            (
                "expression.invalid",
                f"{where} 的动态表达式形状错误；只允许 $ref、$field、$request 或 $op+args",
            )
        )
    return errors


def _valid_field_path(path: str) -> bool:
    if not path or len(path) > MAX_FIELD_PATH_CHARS:
        return False
    parts = path.split(".")
    return len(parts) <= 8 and all(_FIELD_SEGMENT_RE.match(part) for part in parts)


__all__ = [
    "OP_ARITY",
    "dynamic_kind",
    "field_paths",
    "has_dynamic",
    "has_relation_expression",
    "is_field_ref",
    "is_step_ref",
    "iter_dynamic_nodes",
    "validate_dynamic",
]
