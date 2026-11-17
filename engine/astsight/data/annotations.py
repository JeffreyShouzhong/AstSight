"""C5a 确定性语义候选：DB 注释、DBML Note 与规则常量折叠。

只做字面量解析，不调用模型，也不把候选当成业务事实：

- 注释里的「值=含义」只生成 `value_semantics`，证据固定为 `inferred`；
- 规则里的 `eq/in` 常量折叠到列上，保留 `rule_keys` 供审核追溯；
- 与数据库枚举（`CHECK IN` / `ENUM`）冲突的候选直接丢弃并告警，不静默采纳。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from contracts import (
    ColumnCatalog,
    ColumnSemantic,
    Evidence,
    RuleOperator,
    RuleSet,
    TableCatalog,
    ValueSemantics,
)

MAX_VALUES_PER_COLUMN = 200
MAX_COMMENT_PAIRS = 100
MAX_VALUE_CHARS = 200
MAX_MEANING_CHARS = 500

_SEMANTIC_KEYWORDS: tuple[tuple[ColumnSemantic, tuple[str, ...]], ...] = (
    (
        ColumnSemantic.AMOUNT,
        ("金额", "价格", "单价", "总价", "费用", "余额", "应付", "实付"),
    ),
    (ColumnSemantic.QUANTITY, ("数量", "库存", "次数", "件数", "个数", "人数")),
    (ColumnSemantic.STATUS, ("状态", "阶段")),
    (ColumnSemantic.BOOLEAN, ("是否", "启用", "禁用", "开关", "标记")),
    (ColumnSemantic.TIMESTAMP, ("时间", "日期", "时刻")),
    (
        ColumnSemantic.ENUM,
        ("类型", "类别", "种类", "渠道", "级别", "等级", "编码", "代码"),
    ),
)

_INT_RE = re.compile(r"^-?\d+$")
_DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_.-]{0,39}$")
_LOWER_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_SEGMENT_SPLIT_RE = re.compile(r"[\n;；,，|]+")
_PAIR_RE = re.compile(r"^\s*(?P<left>[^=:：]{1,80}?)\s*[=:：]\s*(?P<right>.{1,120}?)\s*$")
_PAREN_RE = re.compile(
    r"^\s*(?P<left>.{1,60}?)\s*[（(]\s*(?P<right>[^（）()]{1,60})\s*[)）]\s*$"
)
_FIELD_LEAF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def semantic_hint(comment: str) -> ColumnSemantic | None:
    """从 DB 注释推断结构语义；没有明确关键词时返回 ``None``，不猜。"""
    text = (comment or "").strip()
    if not text:
        return None
    for semantic, keywords in _SEMANTIC_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return semantic
    return None


def comment_value_pairs(comment: str) -> list[tuple[Any, str]]:
    """解析注释中的「值 → 含义」对。

    只接受一侧是可识别的**字面量**（数字、布尔、大写编码、引号包裹的字符串），
    另一侧是含义，避免把 ``状态: 订单状态`` 之类的说明误认成取值映射。
    """
    out: list[tuple[Any, str]] = []
    seen: set[str] = set()
    for segment in _SEGMENT_SPLIT_RE.split(comment or ""):
        text = segment.strip()
        if not text or len(text) > 220:
            continue
        match = _PAREN_RE.match(text)
        if match is not None:
            pair = _pair_from_sides(match.group("left"), match.group("right"))
        else:
            match = _PAIR_RE.match(text)
            if match is None:
                continue
            pair = _pair_from_sides(match.group("left"), match.group("right"))
        if pair is None:
            continue
        value, meaning = pair
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append((value, meaning))
        if len(out) >= MAX_COMMENT_PAIRS:
            break
    return out


def _pair_from_sides(left: str, right: str) -> tuple[Any, str] | None:
    left_token = _value_token(left)
    right_token = _value_token(right)
    if left_token is not None and left_token[1] == "strong":
        value, meaning = left_token[0], right
    elif right_token is not None and right_token[1] == "strong":
        value, meaning = right_token[0], left
    elif left_token is not None and right_token is None:
        # 小写编码 + 纯文本含义，例如 `pending=待支付`。
        value, meaning = left_token[0], right
    elif right_token is not None and left_token is None:
        value, meaning = right_token[0], left
    else:
        # 两侧都像编码（`status: active`）或都不是字面量时，无法判断方向。
        return None
    cleaned = _clean_meaning(meaning)
    if not cleaned:
        cleaned = str(value)
    return value, cleaned[:MAX_MEANING_CHARS]


def _value_token(token: str) -> tuple[Any, str] | None:
    """返回 ``(值, 强度)``；强度 ``weak`` 表示仅小写编码，需要另一侧是含义。"""
    raw = (token or "").strip()
    quoted = (
        len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"', "`"}
    )
    if quoted:
        body = raw[1:-1].strip()
        # 引号包住的长句更像含义而不是取值；避免把「说明='……'」反向解析。
        if (
            body
            and len(body) <= 64
            and not any(char in body for char in "。！？;；")
        ):
            return body, "strong"
        return None
    if not raw or len(raw) > MAX_VALUE_CHARS:
        return None
    low = raw.lower()
    if low in {"true", "false"}:
        return low == "true", "strong"
    if _INT_RE.match(raw):
        # 保留 01/001 这类编码形态，避免把前导零吞成整数。
        if len(raw.lstrip("-")) > 1 and raw.lstrip("-").startswith("0"):
            return raw, "strong"
        try:
            return int(raw), "strong"
        except ValueError:
            return None
    if _DECIMAL_RE.match(raw):
        try:
            return float(raw), "strong"
        except ValueError:
            return None
    if _CODE_RE.match(raw):
        return raw, "strong"
    if _LOWER_CODE_RE.match(raw):
        return raw, "weak"
    return None


def _clean_meaning(text: str) -> str:
    value = (text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        value = value[1:-1].strip()
    return " ".join(value.split())


def value_semantics_for(
    *,
    table: str,
    column: str,
    comment: str,
    enum_values: Iterable[Any],
    noted_meanings: dict[str, str] | None,
    warnings: list[str],
) -> list[ValueSemantics]:
    """合并三类取值语义来源：DB 注释、DBML Note 与数据库枚举。"""
    enums = list(enum_values)
    enum_keys = {str(value) for value in enums}
    noted = dict(noted_meanings or {})
    meanings: dict[str, tuple[Any, str, str]] = {}
    for value, meaning in comment_value_pairs(comment):
        key = str(value)
        if enum_keys and key not in enum_keys:
            warnings.append(f"comment_value_unknown:{table}.{column}:{key}")
            continue
        meanings.setdefault(key, (value, meaning, "comment"))

    out: list[ValueSemantics] = []
    seen: set[str] = set()
    for value in enums:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        meaning, source = _meaning_for(key, meanings, noted, value)
        out.append(
            ValueSemantics(
                value=value,
                meaning=meaning,
                evidence=Evidence.INFERRED,
                source=source,
            )
        )
    for key, (value, meaning, source) in meanings.items():
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ValueSemantics(
                value=value,
                meaning=meaning,
                evidence=Evidence.INFERRED,
                source=source,
            )
        )
    for key, meaning in noted.items():
        if key in seen or not meaning:
            continue
        seen.add(key)
        out.append(
            ValueSemantics(
                value=key,
                meaning=meaning[:MAX_MEANING_CHARS],
                evidence=Evidence.INFERRED,
                source="dbml",
            )
        )
    return out[:MAX_VALUES_PER_COLUMN]


def _meaning_for(
    key: str,
    meanings: dict[str, tuple[Any, str, str]],
    noted: dict[str, str],
    value: Any,
) -> tuple[str, str]:
    pair = meanings.get(key)
    if pair is not None:
        return pair[1], pair[2]
    note = noted.get(key)
    if note:
        return str(note)[:MAX_MEANING_CHARS], "dbml"
    return str(value), "enum"


def fold_rule_constants(
    tables: Iterable[TableCatalog],
    ruleset: RuleSet | None,
) -> list[str]:
    """把规则里的 `eq/in` 常量折叠成列的取值候选。

    只认能唯一映射到某张表某一列的字段；同名列跨表出现时放弃折叠并告警，
    不用「猜表」的方式制造候选。
    """
    if ruleset is None:
        return []
    table_list = list(tables)
    targets: dict[str, list[tuple[TableCatalog, ColumnCatalog]]] = {}
    for table in table_list:
        for column in table.columns:
            targets.setdefault(column.name.lower(), []).append((table, column))
    table_index = {table.name: table for table in table_list}
    warnings: list[str] = []
    for rule in ruleset.rules:
        for constraint in rule.constraints:
            if constraint.operator not in {RuleOperator.EQ, RuleOperator.IN}:
                continue
            if not constraint.values:
                continue
            matches = _resolve_field(constraint.field, targets, table_index)
            if not matches:
                continue
            if len(matches) > 1:
                warnings.append(
                    f"constant_fold_ambiguous:{constraint.field}:{len(matches)}"
                )
                continue
            table, column = matches[0]
            for raw in constraint.values:
                text = str(raw).strip()
                if not text or text.upper() in {"NULL", "NONE"}:
                    continue
                if len(text) > MAX_VALUE_CHARS:
                    continue
                value = _coerce_constant(text, column)
                if column.enum_values and not any(
                    str(item) == str(value) for item in column.enum_values
                ):
                    warnings.append(
                        "constant_fold_enum_mismatch:"
                        f"{table.name}.{column.name}:{text}"
                    )
                    continue
                meaning = _rule_meaning(constraint.condition, rule.text)
                merge_value(
                    table.name, column, value, meaning, rule.rule_key, warnings
                )
    return list(dict.fromkeys(warnings))


def _resolve_field(
    field: str,
    targets: dict[str, list[tuple[TableCatalog, ColumnCatalog]]],
    table_index: dict[str, TableCatalog],
) -> list[tuple[TableCatalog, ColumnCatalog]]:
    raw = (field or "").strip()
    if not raw:
        return []
    parts = [part for part in raw.split(".") if part]
    if len(parts) >= 2:
        table = table_index.get(parts[-2])
        if table is not None:
            column = table.column_index().get(parts[-1])
            if column is not None:
                return [(table, column)]
    leaf = parts[-1] if parts else ""
    if not _FIELD_LEAF_RE.match(leaf):
        return []
    return list(targets.get(leaf.lower(), []))


def _coerce_constant(text: str, column: ColumnCatalog) -> Any:
    upper_type = (column.type or "").upper()
    if _INT_RE.match(text) and any(
        token in upper_type for token in ("INT", "SERIAL")
    ):
        if len(text.lstrip("-")) > 1 and text.lstrip("-").startswith("0"):
            return text
        try:
            return int(text)
        except ValueError:
            return text
    if _DECIMAL_RE.match(text) and any(
        token in upper_type
        for token in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")
    ):
        try:
            return float(text)
        except ValueError:
            return text
    low = text.lower()
    if low in {"true", "false"} and "BOOL" in upper_type:
        return low == "true"
    return text


def _rule_meaning(condition: str, rule_text: str) -> str:
    meaning = " ".join((condition or "").split()) or " ".join((rule_text or "").split())
    return (meaning or "规则常量")[:MAX_MEANING_CHARS]


def merge_value(
    table_name: str,
    column: ColumnCatalog,
    value: Any,
    meaning: str,
    rule_key: str,
    warnings: list[str],
    *,
    source: str = "rule",
) -> None:
    for item in column.value_semantics:
        if (type(item.value).__name__, str(item.value)) != (
            type(value).__name__,
            str(value),
        ):
            continue
        if rule_key and rule_key not in item.rule_keys:
            if len(item.rule_keys) >= 50:
                warnings.append(
                    f"constant_fold_rule_limit:{table_name}.{column.name}:{rule_key}"
                )
                return
            item.rule_keys.append(rule_key)
        if item.meaning == str(item.value) and meaning:
            item.meaning = meaning
            item.source = source
        elif not item.source:
            item.source = source
        return
    if len(column.value_semantics) >= MAX_VALUES_PER_COLUMN:
        warnings.append(
            f"constant_fold_value_limit:{table_name}.{column.name}"
        )
        return
    column.value_semantics.append(
        ValueSemantics(
            value=value,
            meaning=meaning,
            evidence=Evidence.INFERRED,
            rule_keys=[rule_key] if rule_key else [],
            source=source,
        )
    )


__all__ = [
    "MAX_VALUES_PER_COLUMN",
    "comment_value_pairs",
    "fold_rule_constants",
    "merge_value",
    "semantic_hint",
    "value_semantics_for",
]
