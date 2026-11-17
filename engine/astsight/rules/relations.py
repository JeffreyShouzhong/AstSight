"""规则关系的确定性校验、身份计算与断言 AST 转换。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterator

from contracts import RuleRelation

from ..cases.expressions import OP_ARITY

RELATION_KINDS = {"body_field", "db_row"}
BODY_OPS = {"eq", "ne", "in", "gte", "lte", "contains"}
DB_OPS = {"eq", "ne", "gte", "lte", "count_eq"}
RESERVED_KEYS = frozenset({"$response", "$request", "$step", "$op"})
MAX_DEPTH = 16
MAX_NODES = 128

_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STEP_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_DB_TARGET_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$"
)


def relation_key(rule_key: str, relation: RuleRelation) -> str:
    raw = json.dumps(
        {"rule_key": rule_key, **relation.content_payload()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "RR-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def relation_dynamic_kind(value: Any) -> str | None:
    if not isinstance(value, dict) or not any(key in value for key in RESERVED_KEYS):
        return None
    if set(value) == {"$response"}:
        return "response"
    if set(value) == {"$request"}:
        return "request"
    if set(value) == {"$step"}:
        return "step"
    if set(value) == {"$op", "args"}:
        return "op"
    return "invalid"


def has_relation_source(value: Any) -> bool:
    if relation_dynamic_kind(value) in {"response", "request", "step"}:
        return True
    if isinstance(value, dict):
        return any(has_relation_source(item) for item in value.values())
    if isinstance(value, list):
        return any(has_relation_source(item) for item in value)
    return False


def iter_relation_nodes(
    value: Any,
    *,
    location: str = "value",
) -> Iterator[tuple[Any, str]]:
    stack: list[tuple[Any, str, int]] = [(value, location, 0)]
    seen = 0
    while stack:
        node, where, depth = stack.pop()
        seen += 1
        if seen > MAX_NODES:
            raise ValueError(f"关系表达式节点超过上限 {MAX_NODES}")
        if depth > MAX_DEPTH:
            raise ValueError(f"关系表达式嵌套超过上限 {MAX_DEPTH}")
        kind = relation_dynamic_kind(node)
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


def validate_relation(relation: RuleRelation) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if relation.assertion_kind not in RELATION_KINDS:
        errors.append(
            (
                "relation.kind_invalid",
                f"assertion_kind 只允许 {sorted(RELATION_KINDS)}",
            )
        )
    allowed_ops = BODY_OPS if relation.assertion_kind == "body_field" else DB_OPS
    if relation.op not in allowed_ops:
        errors.append(
            (
                "relation.op_invalid",
                f"{relation.assertion_kind} 不支持 op={relation.op!r}",
            )
        )
    if relation.assertion_kind == "body_field":
        if not _valid_path(relation.target) or "." in relation.target:
            errors.append(
                (
                    "relation.target_invalid",
                    "body_field target 必须是响应顶层字段；嵌套取值写在表达式里",
                )
            )
        if relation.where:
            errors.append(
                ("relation.where_unexpected", "body_field 关系不接受 where")
            )
        if relation.wait_ms:
            errors.append(
                ("relation.wait_unexpected", "body_field 关系不接受 wait_ms")
            )
    elif not _DB_TARGET_RE.match(relation.target):
        errors.append(
            (
                "relation.target_invalid",
                "db_row target 必须是 table.column",
            )
        )
    if not has_relation_source(relation.expect) and not has_relation_source(
        relation.where
    ):
        errors.append(
            (
                "relation.not_dynamic",
                "关系必须至少引用 $response、$request 或 $step；固定值不构成关系",
            )
        )
    try:
        for node, where in iter_relation_nodes(relation.expect, location="expect"):
            errors.extend(_validate_node(node, where))
        for node, where in iter_relation_nodes(relation.where, location="where"):
            errors.extend(_validate_node(node, where))
    except ValueError as exc:
        errors.append(("relation.too_complex", str(exc)))
    return errors


def _validate_node(node: dict[str, Any], where: str) -> list[tuple[str, str]]:
    kind = relation_dynamic_kind(node)
    if kind in {"response", "request"}:
        path = node.get(f"${kind}")
        if not isinstance(path, str) or not _valid_path(path):
            return [
                (
                    "relation.path_invalid",
                    f"{where} 的 ${kind} 必须是点分隔字段路径",
                )
            ]
        return []
    if kind == "step":
        ref = node.get("$step")
        if not isinstance(ref, str) or len(ref) > 256 or "." not in ref:
            return [
                (
                    "relation.step_ref_invalid",
                    f"{where} 的 $step 必须是 '<别名>.<字段>'",
                )
            ]
        alias, _, field = ref.partition(".")
        if not STEP_ALIAS_RE.match(alias) or not _valid_path(field):
            return [
                (
                    "relation.step_ref_invalid",
                    f"{where} 的 $step 别名或字段非法: {ref!r}",
                )
            ]
        return []
    if kind == "op":
        op = node.get("$op")
        args = node.get("args")
        if not isinstance(op, str) or op not in OP_ARITY:
            return [
                (
                    "relation.op_expression_invalid",
                    f"{where} 不支持运算 {op!r}",
                )
            ]
        if not isinstance(args, list):
            return [("relation.args_invalid", f"{where}.args 必须是列表")]
        low, high = OP_ARITY[op]
        if not low <= len(args) <= high:
            expected = str(low) if low == high else f"{low}..{high}"
            return [
                (
                    "relation.args_invalid",
                    f"{where} 的 {op} 需要 {expected} 个参数",
                )
            ]
        return []
    return [
        (
            "relation.expression_invalid",
            f"{where} 只允许 $response、$request、$step 或 $op+args",
        )
    ]


def relation_step_aliases(relation: RuleRelation) -> list[str]:
    """返回关系中 ``$step`` 用到的语义别名（按出现顺序去重）。"""
    out: list[str] = []
    for value in (relation.expect, relation.where):
        try:
            nodes = iter_relation_nodes(value)
            for node, _where in nodes:
                if relation_dynamic_kind(node) != "step":
                    continue
                alias = str(node.get("$step", "")).partition(".")[0]
                if alias and alias not in out:
                    out.append(alias)
        except ValueError:
            # 复杂度过高已由 validate_relation 报出，这里不重复报错。
            continue
    return out


def to_assertion_expression(
    value: Any,
    *,
    step_map: dict[str, str] | None = None,
) -> Any:
    """把规则表达式转换为断言表达式。

    ``$response`` 映射为当前响应 ``$field``；``$step`` 按 A4 的绑定把语义别名
    换成具体步骤名，生成断言侧 ``$ref``。
    """
    if isinstance(value, dict):
        if set(value) == {"$response"}:
            return {"$field": value["$response"]}
        if set(value) == {"$request"}:
            return {"$request": value["$request"]}
        if set(value) == {"$step"}:
            alias, _, field = str(value["$step"]).partition(".")
            step_name = (step_map or {}).get(alias)
            if not step_name:
                raise ValueError(f"未绑定的步骤别名: {alias!r}")
            return {"$ref": f"{step_name}.{field}"}
        if set(value) == {"$op", "args"}:
            return {
                "$op": value["$op"],
                "args": [
                    to_assertion_expression(item, step_map=step_map)
                    for item in value["args"]
                ],
            }
        return {
            key: to_assertion_expression(item, step_map=step_map)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [to_assertion_expression(item, step_map=step_map) for item in value]
    return value


def relation_literal_tokens(value: Any) -> list[str]:
    """提取关系中的字面量，用于 quote grounding。"""
    out: list[str] = []
    if isinstance(value, dict):
        kind = relation_dynamic_kind(value)
        if kind in {"response", "request", "step"}:
            return out
        if kind == "op":
            return relation_literal_tokens(value.get("args") or [])
        if kind == "invalid":
            return out
        for item in value.values():
            out.extend(relation_literal_tokens(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(relation_literal_tokens(item))
    elif value is not None and not isinstance(value, bool):
        out.append(str(value))
    return out


def _valid_path(path: str) -> bool:
    if not path or len(path) > 256:
        return False
    parts = path.split(".")
    return len(parts) <= 8 and all(_PATH_SEGMENT_RE.match(part) for part in parts)


__all__ = [
    "BODY_OPS",
    "DB_OPS",
    "has_relation_source",
    "iter_relation_nodes",
    "relation_dynamic_kind",
    "relation_key",
    "relation_literal_tokens",
    "relation_step_aliases",
    "to_assertion_expression",
    "validate_relation",
]
