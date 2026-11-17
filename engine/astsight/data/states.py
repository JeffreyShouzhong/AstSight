"""C5a 跨表业务态候选：规则约束 + 外键图 → 可校验的 DataState 骨架。

只处理能唯一映射到目录列的 `eq/in` 约束；同一条件下至少覆盖两张表才生成。
主键、外键和被外键牵连的父表由框架补确定性占位值；其余 NOT NULL 列也用占位值
补足，并在 `gaps` 里逐条列出，明确告诉审核人哪些值是机制补的，不是业务事实。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from contracts import (
    ColumnCatalog,
    DataState,
    Evidence,
    ForeignKey,
    Rule,
    RuleConstraint,
    RuleOperator,
    RuleSet,
    TableCatalog,
)

from .annotations import coerce_constant, resolve_field_targets
from .validate import check_type, validate_plan_fixtures

MAX_STATES = 200
MAX_STATE_COMBINATIONS = 20
_PLACEHOLDER_TIMESTAMP = "2026-01-01T00:00:00"


@dataclass
class StateProposal:
    states: list[DataState] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)


def propose_rule_states(
    tables: Iterable[TableCatalog],
    ruleset: RuleSet | None,
) -> StateProposal:
    if ruleset is None:
        return StateProposal()
    table_list = list(tables)
    proposal = StateProposal()
    for rule in ruleset.rules:
        _propose_transitions(rule, table_list, proposal)
        for condition, constraints in _constraint_groups(rule).items():
            if len(proposal.states) >= MAX_STATES:
                proposal.warnings.append(f"rule_state_limit:{MAX_STATES}")
                return proposal
            _propose_one(
                rule, condition, constraints, table_list, proposal
            )
    proposal.warnings = list(dict.fromkeys(proposal.warnings))
    return proposal


def _constraint_groups(
    rule: Rule,
) -> dict[str, list[tuple[RuleConstraint, list[str]]]]:
    groups: dict[str, list[tuple[RuleConstraint, list[str]]]] = {}
    for constraint in rule.constraints:
        if constraint.operator not in {RuleOperator.EQ, RuleOperator.IN}:
            continue
        values = [
            str(value).strip()
            for value in constraint.values
            if str(value).strip()
            and str(value).strip().upper() not in {"NULL", "NONE"}
        ]
        if not values:
            continue
        groups.setdefault(constraint.condition or "", []).append(
            (constraint, values)
        )
    return groups


def _propose_transitions(
    rule: Rule,
    tables: list[TableCatalog],
    proposal: StateProposal,
) -> None:
    groups: dict[str, list[RuleConstraint]] = {}
    for constraint in rule.constraints:
        if constraint.operator != RuleOperator.TRANSITION:
            continue
        if len(constraint.values) < 2:
            continue
        groups.setdefault(constraint.condition or "", []).append(constraint)
    if not groups:
        return
    eq_groups = _constraint_groups(rule)
    for condition, transitions in groups.items():
        if len(proposal.states) >= MAX_STATES:
            proposal.warnings.append(f"rule_state_limit:{MAX_STATES}")
            return
        before: dict[str, dict[str, Any]] = {}
        target: dict[str, dict[str, Any]] = {}
        for constraint in transitions:
            matches = resolve_field_targets(tables, constraint.field)
            if not matches:
                continue
            if len(matches) > 1:
                proposal.warnings.append(
                    f"rule_state_field_ambiguous:"
                    f"{constraint.field}:{len(matches)}"
                )
                return
            table, column = matches[0]
            raw_before = str(constraint.values[0]).strip()
            raw_after = str(constraint.values[1]).strip()
            before_value = _transition_value(raw_before, column)
            after_value = _transition_value(raw_after, column)
            for raw, value in (
                (raw_before, before_value),
                (raw_after, after_value),
            ):
                if column.enum_values and value is not None and not any(
                    str(item) == str(value) for item in column.enum_values
                ):
                    proposal.warnings.append(
                        f"rule_state_enum_mismatch:"
                        f"{table.name}.{column.name}:{raw}"
                    )
                    return
            existing = before.setdefault(table.name, {}).get(column.name)
            if existing is not None and str(existing) != str(before_value):
                proposal.warnings.append(
                    f"rule_state_value_conflict:{table.name}.{column.name}"
                )
                return
            before[table.name][column.name] = before_value
            target.setdefault(table.name, {})[column.name] = after_value

        for constraint, values in eq_groups.get(condition, []):
            matches = resolve_field_targets(tables, constraint.field)
            if len(matches) != 1:
                continue
            table, column = matches[0]
            value = coerce_constant(values[0], column)
            if column.enum_values and not any(
                str(item) == str(value) for item in column.enum_values
            ):
                proposal.warnings.append(
                    f"rule_state_enum_mismatch:"
                    f"{table.name}.{column.name}:{values[0]}"
                )
                return
            current = before.setdefault(table.name, {})
            existing = current.get(column.name)
            if existing is not None and str(existing) != str(value):
                proposal.warnings.append(
                    f"rule_state_value_conflict:{table.name}.{column.name}"
                )
                return
            current[column.name] = value

        if not before:
            return
        _build_state(
            rule,
            condition,
            before,
            tables,
            proposal,
            target_state=target,
            prefix="transition",
        )


def _transition_value(raw: str, column: ColumnCatalog) -> Any:
    if raw.upper() in {"NULL", "NONE"}:
        return None
    return coerce_constant(raw, column)


def _propose_one(
    rule: Rule,
    condition: str,
    constraints: list[tuple[RuleConstraint, list[str]]],
    tables: list[TableCatalog],
    proposal: StateProposal,
) -> None:
    options: list[tuple[TableCatalog, ColumnCatalog, list[Any]]] = []
    for constraint, values in constraints:
        matches = resolve_field_targets(tables, constraint.field)
        if not matches:
            continue
        if len(matches) > 1:
            proposal.warnings.append(
                f"rule_state_field_ambiguous:{constraint.field}:{len(matches)}"
            )
            return
        table, column = matches[0]
        coerced: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for raw in values:
            value = coerce_constant(raw, column)
            if column.enum_values and not any(
                str(item) == str(value) for item in column.enum_values
            ):
                proposal.warnings.append(
                    f"rule_state_enum_mismatch:"
                    f"{table.name}.{column.name}:{raw}"
                )
                return
            key = (type(value).__name__, str(value))
            if key in seen:
                continue
            seen.add(key)
            coerced.append(value)
        if coerced:
            options.append((table, column, coerced))

    if len({table.name for table, _column, _values in options}) < 2:
        return

    total = math.prod(len(values) for _table, _column, values in options)
    iterator = itertools.product(
        *(values for _table, _column, values in options)
    )
    combos = list(itertools.islice(iterator, MAX_STATE_COMBINATIONS))
    if total > MAX_STATE_COMBINATIONS:
        proposal.gaps.append(
            {
                "kind": "state_value_combinations_truncated",
                "rule_key": rule.rule_key,
                "condition": condition,
                "reason": (
                    f"取值组合共 {total} 个，只展开前 "
                    f"{MAX_STATE_COMBINATIONS} 个"
                ),
            }
        )
    for combo in combos:
        pinned: dict[str, dict[str, Any]] = {}
        for (table, column, _values), value in zip(options, combo):
            pinned.setdefault(table.name, {})[column.name] = value
        _build_state(rule, condition, pinned, tables, proposal)


def _build_state(
    rule: Rule,
    condition: str,
    pinned: dict[str, dict[str, Any]],
    tables: list[TableCatalog],
    proposal: StateProposal,
    *,
    target_state: dict[str, dict[str, Any]] | None = None,
    prefix: str = "rule",
) -> None:
    table_index = {table.name: table for table in tables}
    key = _state_key(rule, condition, pinned, target_state, prefix)
    rows = {table: dict(values) for table, values in pinned.items()}
    placeholders: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    fks = [fk for table in tables for fk in table.foreign_keys]
    by_child: dict[str, list[ForeignKey]] = {}
    for fk in fks:
        by_child.setdefault(fk.child_table, []).append(fk)

    try:
        for table_name in list(rows):
            _ensure_table(
                table_name,
                rows,
                table_index,
                by_child,
                set(),
                key,
                placeholders,
                warnings,
            )
        for table_name, row in rows.items():
            table = table_index[table_name]
            _fill_not_null(key, table, row, placeholders, warnings)
    except _StateError as exc:
        proposal.warnings.append(f"rule_state_invalid:{key}:{exc}")
        return

    target = target_state or {}
    target_errors = _validate_target(tables, target)
    if target_errors:
        proposal.warnings.append(
            f"rule_state_invalid:{key}:{';'.join(target_errors[:5])}"
        )
        return
    state = DataState(
        state_key=key,
        target_state=target,
        table_values={table: [row] for table, row in rows.items()},
        evidence=Evidence.INFERRED,
        rule_keys=[rule.rule_key],
    )
    errors = validate_plan_fixtures(
        _catalog_view(tables), state.table_values
    )
    if errors:
        proposal.warnings.append(
            f"rule_state_invalid:{key}:{';'.join(errors[:5])}"
        )
        return
    proposal.states.append(state)
    for table, column, reason in placeholders:
        proposal.gaps.append(
            {
                "kind": "state_placeholder",
                "state_key": key,
                "table": table,
                "column": column,
                "reason": reason,
            }
        )
    for warning in warnings:
        proposal.warnings.append(f"{key}:{warning}")


def _validate_target(
    tables: list[TableCatalog],
    target: dict[str, dict[str, Any]],
) -> list[str]:
    index = {table.name: table for table in tables}
    errors: list[str] = []
    for table_name, values in target.items():
        table = index.get(table_name)
        if table is None:
            errors.append(f"target_table_unknown:{table_name}")
            continue
        columns = table.column_index()
        for column_name, value in values.items():
            column = columns.get(column_name)
            if column is None:
                errors.append(f"target_column_unknown:{table_name}.{column_name}")
                continue
            if value is None:
                if not column.nullable:
                    errors.append(
                        f"target_not_null:{table_name}.{column_name}"
                    )
                continue
            if not isinstance(value, (str, int, float, bool)):
                errors.append(
                    f"target_value_not_scalar:{table_name}.{column_name}"
                )
                continue
            if not check_type(column.type, value):
                errors.append(
                    f"target_type_mismatch:{table_name}.{column_name}"
                )
                continue
            if column.enum_values and not any(
                str(item) == str(value) for item in column.enum_values
            ):
                errors.append(
                    f"target_enum_mismatch:{table_name}.{column_name}:{value}"
                )
    return errors


class _StateError(ValueError):
    pass


def _ensure_table(
    table_name: str,
    rows: dict[str, dict[str, Any]],
    table_index: dict[str, TableCatalog],
    by_child: dict[str, list[ForeignKey]],
    visiting: set[str],
    state_key: str,
    placeholders: list[tuple[str, str, str]],
    warnings: list[str],
) -> None:
    table = table_index.get(table_name)
    if table is None:
        raise _StateError(f"unknown_table:{table_name}")
    if table_name in visiting:
        raise _StateError(f"fk_cycle:{table_name}")
    row = rows.setdefault(table_name, {})
    _ensure_primary_key(table, row, state_key)
    visiting.add(table_name)
    try:
        for fk in by_child.get(table_name, []):
            if len(fk.child_columns) != 1 or len(fk.parent_columns) != 1:
                raise _StateError(f"composite_fk_unsupported:{table_name}")
            child_column = table.column_index()[fk.child_columns[0]]
            if fk.child_columns[0] in row:
                continue
            if fk.parent_table not in rows and child_column.nullable:
                continue
            _ensure_table(
                fk.parent_table,
                rows,
                table_index,
                by_child,
                visiting,
                state_key,
                placeholders,
                warnings,
            )
            parent_row = rows[fk.parent_table]
            parent_column = fk.parent_columns[0]
            if parent_column not in parent_row:
                raise _StateError(
                    f"fk_parent_value_missing:{fk.parent_table}.{parent_column}"
                )
            row[fk.child_columns[0]] = parent_row[parent_column]
    finally:
        visiting.discard(table_name)


def _ensure_primary_key(
    table: TableCatalog,
    row: dict[str, Any],
    state_key: str,
) -> None:
    if len(table.primary_key) != 1:
        raise _StateError(f"primary_key_unsupported:{table.name}")
    name = table.primary_key[0]
    if name in row:
        return
    column = table.column_index().get(name)
    if column is None:
        raise _StateError(f"primary_key_missing:{table.name}.{name}")
    value = _primary_key_value(table, column, state_key)
    if value is None:
        raise _StateError(f"primary_key_type_unsupported:{table.name}.{name}")
    row[name] = value


def _primary_key_value(
    table: TableCatalog,
    column: ColumnCatalog,
    state_key: str,
) -> Any:
    upper_type = (column.type or "").upper()
    if any(token in upper_type for token in ("INT", "SERIAL")):
        return 1
    if any(
        token in upper_type
        for token in ("CHAR", "TEXT", "CLOB", "UUID", "ENUM")
    ):
        safe = "".join(
            char if char.isalnum() else "_" for char in table.name
        )
        return f"ast_{safe}_1"[:200]
    return None


def _fill_not_null(
    state_key: str,
    table: TableCatalog,
    row: dict[str, Any],
    placeholders: list[tuple[str, str, str]],
    warnings: list[str],
) -> None:
    for column in table.columns:
        if column.name in row:
            continue
        if column.nullable or column.default is not None:
            continue
        if column.primary_key or _is_fk_column(table, column.name):
            raise _StateError(f"required_key_missing:{table.name}.{column.name}")
        value, reason = _placeholder(column)
        if value is None:
            raise _StateError(
                f"placeholder_unsupported:{table.name}.{column.name}"
            )
        row[column.name] = value
        placeholders.append((table.name, column.name, reason))


def _is_fk_column(table: TableCatalog, name: str) -> bool:
    return any(
        name in fk.child_columns for fk in table.foreign_keys
    )


def _placeholder(column: ColumnCatalog) -> tuple[Any, str]:
    if column.enum_values:
        value = column.enum_values[0]
        return value, "NOT NULL 列使用数据库枚举首值作为占位"
    upper_type = (column.type or "").upper()
    if any(token in upper_type for token in ("INT", "SERIAL")):
        return 1, "NOT NULL 数值列使用确定性占位值 1"
    if any(
        token in upper_type
        for token in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")
    ):
        return 0.0, "NOT NULL 数值列使用确定性占位值 0"
    if "BOOL" in upper_type:
        return False, "NOT NULL 布尔列使用确定性占位值 false"
    if any(token in upper_type for token in ("TIME", "DATE")):
        return _PLACEHOLDER_TIMESTAMP, "NOT NULL 时间列使用固定占位时间"
    if any(
        token in upper_type
        for token in ("CHAR", "TEXT", "CLOB", "UUID", "ENUM")
    ):
        return f"ast_{column.name}"[:200], "NOT NULL 文本列使用确定性占位值"
    return None, ""


def _state_key(
    rule: Rule,
    condition: str,
    pinned: dict[str, dict[str, Any]],
    target_state: dict[str, dict[str, Any]] | None = None,
    prefix: str = "rule",
) -> str:
    raw = json.dumps(
        {
            "rule": rule.rule_key,
            "condition": condition,
            "pinned": pinned,
            "target": target_state or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    label = rule.rule_id or rule.rule_key.replace("RK-", "")
    label = "".join(
        char if char.isalnum() or char == "_" else "_" for char in label
    )
    return f"{prefix}_{label}_{digest}"


class _CatalogView:
    """只给 validate_plan_fixtures 用的最小目录视图。"""

    def __init__(self, tables: list[TableCatalog]) -> None:
        self.tables = tables
        self.foreign_keys = [
            fk for table in tables for fk in table.foreign_keys
        ]

    def table_index(self) -> dict[str, TableCatalog]:
        return {table.name: table for table in self.tables}


def _catalog_view(tables: list[TableCatalog]) -> _CatalogView:
    return _CatalogView(tables)


__all__ = ["MAX_STATES", "StateProposal", "propose_rule_states"]
