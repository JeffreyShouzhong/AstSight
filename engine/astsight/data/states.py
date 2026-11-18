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
    StatePath,
    StatePathStep,
    TableCatalog,
)

from .annotations import coerce_constant, resolve_field_group
from .validate import check_type, validate_plan_fixtures

MAX_STATES = 200
MAX_STATE_COMBINATIONS = 20
MAX_STATE_PATHS = 50
MAX_PATH_STEPS = 5
_PLACEHOLDER_TIMESTAMP = "2026-01-01T00:00:00"


@dataclass
class StateProposal:
    states: list[DataState] = field(default_factory=list)
    paths: list[StatePath] = field(default_factory=list)
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
        _propose_pair_transitions(rule, table_list, proposal)
        for condition, constraints in _constraint_groups(rule).items():
            if len(proposal.states) >= MAX_STATES:
                proposal.warnings.append(f"rule_state_limit:{MAX_STATES}")
                return proposal
            _propose_one(
                rule, condition, constraints, table_list, proposal
            )
    _build_paths(table_list, ruleset, proposal)
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
        eq_items = eq_groups.get(condition, [])
        all_constraints = [
            *transitions,
            *(constraint for constraint, _values in eq_items),
        ]
        resolved, ambiguous = resolve_field_group(
            tables, [constraint.field for constraint in all_constraints]
        )
        for field, count in ambiguous:
            proposal.warnings.append(
                f"rule_state_field_ambiguous:{field}:{count}"
            )
        if ambiguous:
            return
        resolved_by_constraint = {
            id(constraint): matches
            for constraint, matches in zip(all_constraints, resolved)
        }
        for constraint in transitions:
            matches = resolved_by_constraint[id(constraint)]
            if not matches:
                continue
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

        for constraint, values in eq_items:
            matches = resolved_by_constraint[id(constraint)]
            if not matches:
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


_PAIR_PREFIXES = (
    ("from_", "to_", "before", "after"),
    ("old_", "new_", "before", "after"),
    ("before_", "after_", "before", "after"),
)


def _pair_key(field: str) -> tuple[str, str] | None:
    leaf = (field or "").split(".")[-1].lower()
    for before_prefix, after_prefix, before_side, after_side in _PAIR_PREFIXES:
        if leaf.startswith(before_prefix) and len(leaf) > len(before_prefix):
            return before_side, leaf[len(before_prefix) :]
        if leaf.startswith(after_prefix) and len(leaf) > len(after_prefix):
            return after_side, leaf[len(after_prefix) :]
    return None


def _propose_pair_transitions(
    rule: Rule,
    tables: list[TableCatalog],
    proposal: StateProposal,
) -> None:
    """从 `from_status`/`to_status` 这类成对字段生成迁移前/迁移后候选。"""
    groups: dict[str, list[RuleConstraint]] = {}
    for constraint in rule.constraints:
        if constraint.operator not in {RuleOperator.EQ, RuleOperator.IN}:
            continue
        if len(constraint.values) != 1 or _pair_key(constraint.field) is None:
            continue
        groups.setdefault(constraint.condition or "", []).append(constraint)
    if not groups:
        return
    for condition, grouped in groups.items():
        if len(proposal.states) >= MAX_STATES:
            proposal.warnings.append(f"rule_state_limit:{MAX_STATES}")
            return
        # paired 约束可能带 NULL 表示迁移前为空，不能用 _constraint_groups（它会过滤 NULL）。
        all_constraints = [
            (constraint, [str(value).strip() for value in constraint.values])
            for constraint in rule.constraints
            if (constraint.condition or "") == condition
            and constraint.operator in {RuleOperator.EQ, RuleOperator.IN}
            and constraint.values
        ]
        resolved, ambiguous = resolve_field_group(
            tables, [constraint.field for constraint, _values in all_constraints]
        )
        for field, count in ambiguous:
            proposal.warnings.append(
                f"rule_state_field_ambiguous:{field}:{count}"
            )
        if ambiguous:
            return
        resolved_by_constraint = {
            id(constraint): matches
            for (constraint, _values), matches in zip(all_constraints, resolved)
        }
        pairs: dict[tuple[str, str], dict[str, tuple[ColumnCatalog, str]]] = {}
        for constraint, _values in all_constraints:
            parsed = _pair_key(constraint.field)
            matches = resolved_by_constraint[id(constraint)]
            if parsed is None or len(matches) != 1:
                continue
            side, base = parsed
            table, column = matches[0]
            pairs.setdefault((table.name, base), {})[side] = (
                column,
                str(constraint.values[0]).strip(),
            )
        for (table_name, _base), sides in pairs.items():
            if "before" not in sides or "after" not in sides:
                continue
            before_column, before_raw = sides["before"]
            after_column, after_raw = sides["after"]
            before_value = _transition_value(before_raw, before_column)
            after_value = _transition_value(after_raw, after_column)
            if not _check_transition_value(
                table_name, before_column, before_raw, before_value, proposal
            ):
                continue
            if not _check_transition_value(
                table_name, after_column, after_raw, after_value, proposal
            ):
                continue
            # 成对字段通常记录一次迁移事件：迁移前行同时保存 from/to 两列，
            # target_state 再声明期望的 to 值；这样不会给 NOT NULL 的 to 列
            # 填一个与 target 冲突的占位值。
            before: dict[str, dict[str, Any]] = {
                table_name: {
                    before_column.name: before_value,
                    after_column.name: after_value,
                }
            }
            target: dict[str, dict[str, Any]] = {
                table_name: {after_column.name: after_value}
            }
            if not _merge_extra_constraints(
                all_constraints,
                resolved_by_constraint,
                before,
                proposal,
            ):
                continue
            _build_state(
                rule,
                condition,
                before,
                tables,
                proposal,
                target_state=target,
                prefix="transition",
            )


def _check_transition_value(
    table_name: str,
    column: ColumnCatalog,
    raw: str,
    value: Any,
    proposal: StateProposal,
) -> bool:
    if value is None:
        return True
    if column.enum_values and not any(
        str(item) == str(value) for item in column.enum_values
    ):
        proposal.warnings.append(
            f"rule_state_enum_mismatch:{table_name}.{column.name}:{raw}"
        )
        return False
    return True


def _merge_extra_constraints(
    constraints: list[tuple[RuleConstraint, list[str]]],
    resolved_by_constraint: dict[int, list[tuple[TableCatalog, ColumnCatalog]]],
    before: dict[str, dict[str, Any]],
    proposal: StateProposal,
) -> bool:
    for constraint, values in constraints:
        if _pair_key(constraint.field) is not None:
            continue
        matches = resolved_by_constraint[id(constraint)]
        if len(matches) != 1:
            continue
        table, column = matches[0]
        value = _transition_value(values[0], column)
        if value is not None and column.enum_values and not any(
            str(item) == str(value) for item in column.enum_values
        ):
            proposal.warnings.append(
                f"rule_state_enum_mismatch:"
                f"{table.name}.{column.name}:{values[0]}"
            )
            return False
        current = before.setdefault(table.name, {})
        existing = current.get(column.name)
        if existing is not None and str(existing) != str(value):
            proposal.warnings.append(
                f"rule_state_value_conflict:{table.name}.{column.name}"
            )
            return False
        current[column.name] = value
    return True


def _propose_one(
    rule: Rule,
    condition: str,
    constraints: list[tuple[RuleConstraint, list[str]]],
    tables: list[TableCatalog],
    proposal: StateProposal,
) -> None:
    options: list[tuple[TableCatalog, ColumnCatalog, list[Any]]] = []
    resolved, ambiguous = resolve_field_group(
        tables, [constraint.field for constraint, _values in constraints]
    )
    for field, count in ambiguous:
        proposal.warnings.append(
            f"rule_state_field_ambiguous:{field}:{count}"
        )
    if ambiguous:
        return
    for (constraint, values), matches in zip(constraints, resolved):
        if not matches:
            continue
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


def expand_state_paths(
    tables: Iterable[TableCatalog],
    paths: Iterable[StatePath],
) -> tuple[list[DataState], list[str]]:
    """把线性状态路径展开为可执行多行 DataState。

    每步的起点/终点按出现顺序去重后各生成一行；同一路径的全部子行引用
    同一组已补齐父行，避免占位父键冲突。
    """
    table_list = list(tables)
    table_index = {table.name: table for table in table_list}
    by_child: dict[str, list[ForeignKey]] = {}
    for table in table_list:
        for fk in table.foreign_keys:
            by_child.setdefault(fk.child_table, []).append(fk)
    states: list[DataState] = []
    warnings: list[str] = []
    for path in paths:
        table = table_index.get(path.table)
        if table is None:
            warnings.append(f"state_path_expand_table_unknown:{path.path_key}")
            continue
        if not table.primary_key or len(table.primary_key) != 1:
            warnings.append(
                f"state_path_expand_primary_key_unsupported:{path.path_key}"
            )
            continue
        columns = table.column_index()
        column = columns.get(path.column)
        paired_from = columns.get(f"from_{path.column}")
        paired_to = columns.get(f"to_{path.column}")
        paired = column is None and paired_from is not None and paired_to is not None
        if column is None and not paired:
            warnings.append(
                f"state_path_expand_column_unknown:{path.path_key}:{path.column}"
            )
            continue
        if table.primary_key[0] == path.column:
            warnings.append(
                f"state_path_expand_primary_key_column:{path.path_key}"
            )
            continue
        row_specs: list[dict[str, Any]] = []
        if paired:
            for step in path.steps:
                row = {
                    paired_from.name: step.from_value,
                    paired_to.name: step.to_value,
                }
                if row not in row_specs:
                    row_specs.append(row)
        else:
            for step in path.steps:
                for value in (step.from_value, step.to_value):
                    row = {path.column: value}
                    if row not in row_specs:
                        row_specs.append(row)
        if not row_specs:
            warnings.append(f"state_path_expand_empty:{path.path_key}")
            continue

        state_key = "state_path_" + hashlib.sha256(
            path.path_key.encode("utf-8")
        ).hexdigest()[:12]
        base_rows: dict[str, dict[str, Any]] | None = None
        path_rows: list[dict[str, Any]] = []
        failed = False
        for index, spec in enumerate(row_specs):
            pk = _path_primary_key(
                table,
                index=index,
                path_key=path.path_key,
            )
            if pk is None:
                warnings.append(
                    f"state_path_expand_primary_key_type:"
                    f"{path.path_key}:{table.primary_key[0]}"
                )
                failed = True
                break
            rows = {
                table.name: {
                    table.primary_key[0]: pk,
                    **spec,
                }
            }
            placeholders: list[tuple[str, str, str]] = []
            row_warnings: list[str] = []
            try:
                _ensure_table(
                    table.name,
                    rows,
                    table_index,
                    by_child,
                    set(),
                    f"{state_key}_{index}",
                    placeholders,
                    row_warnings,
                )
                for table_name, row in rows.items():
                    _fill_not_null(
                        f"{state_key}_{index}",
                        table_index[table_name],
                        row,
                        placeholders,
                        row_warnings,
                    )
            except _StateError as exc:
                warnings.append(
                    f"state_path_expand_invalid:{path.path_key}:{exc}"
                )
                failed = True
                break
            if base_rows is None:
                base_rows = {
                    name: dict(row)
                    for name, row in rows.items()
                    if name != table.name
                }
            path_row = dict(rows[table.name])
            if base_rows is not None:
                for fk in by_child.get(table.name, []):
                    parent = base_rows.get(fk.parent_table)
                    if parent is None:
                        continue
                    for child_name, parent_name in zip(
                        fk.child_columns,
                        fk.parent_columns,
                        strict=True,
                    ):
                        path_row[child_name] = parent.get(parent_name)
            path_rows.append(path_row)
            warnings.extend(
                f"{path.path_key}:{item}" for item in row_warnings
            )
        if failed or base_rows is None or not path_rows:
            continue
        table_values = {
            name: [dict(row)] for name, row in base_rows.items()
        }
        table_values[table.name] = path_rows
        errors = validate_plan_fixtures(
            _catalog_view(table_list),
            table_values,
        )
        if errors:
            warnings.extend(
                f"state_path_expand_invalid:{path.path_key}:{message}"
                for message in errors[:10]
            )
            continue
        states.append(
            DataState(
                state_key=state_key,
                table_values=table_values,
                target_state={
                    path.table: (
                        {paired_to.name: path.steps[-1].to_value}
                        if paired
                        else {path.column: row_specs[-1][path.column]}
                    ),
                },
                evidence=Evidence.INFERRED,
                rule_keys=list(
                    dict.fromkeys(
                        step.rule_key for step in path.steps if step.rule_key
                    )
                ),
            )
        )
    return states, list(dict.fromkeys(warnings))


def _path_primary_key(
    table: TableCatalog,
    *,
    index: int,
    path_key: str,
) -> Any:
    name = table.primary_key[0]
    column = table.column_index().get(name)
    if column is None:
        return None
    upper = (column.type or "").upper()
    if any(token in upper for token in ("INT", "SERIAL")):
        return index + 1
    if "UUID" in upper:
        return f"00000000-0000-0000-0000-{index + 1:012d}"
    if any(token in upper for token in ("CHAR", "TEXT", "CLOB", "ENUM")):
        digest = hashlib.sha256(path_key.encode("utf-8")).hexdigest()[:8]
        return f"P-{digest}-{index + 1}"
    return None


@dataclass(frozen=True)
class _Edge:
    table: str
    column: str
    before_key: str
    after_key: str
    before_value: Any
    after_value: Any
    rule_key: str
    condition: str


def _build_paths(
    tables: list[TableCatalog],
    ruleset: RuleSet,
    proposal: StateProposal,
) -> None:
    edges: dict[tuple[str, str, str, str], _Edge] = {}
    for rule in ruleset.rules:
        _collect_transition_edges(rule, tables, edges, proposal)
        _collect_pair_edges(rule, tables, edges, proposal)
    grouped: dict[tuple[str, str], list[_Edge]] = {}
    for edge in edges.values():
        grouped.setdefault((edge.table, edge.column), []).append(edge)
    paths: list[StatePath] = []
    for (table, column), group in sorted(grouped.items()):
        for edges_path in _find_multi_step_paths(group, proposal):
            paths.append(_state_path(table, column, edges_path))
            if len(paths) >= MAX_STATE_PATHS:
                proposal.warnings.append(f"state_path_limit:{MAX_STATE_PATHS}")
                proposal.paths = paths
                return
    proposal.paths = paths


def _collect_transition_edges(
    rule: Rule,
    tables: list[TableCatalog],
    edges: dict[tuple[str, str, str, str], _Edge],
    proposal: StateProposal,
) -> None:
    groups: dict[str, list[RuleConstraint]] = {}
    for constraint in rule.constraints:
        if constraint.operator != RuleOperator.TRANSITION:
            continue
        if len(constraint.values) < 2:
            continue
        groups.setdefault(constraint.condition or "", []).append(constraint)
    for condition, transitions in groups.items():
        others = [
            constraint
            for constraint in rule.constraints
            if (constraint.condition or "") == condition
            and constraint.operator in {RuleOperator.EQ, RuleOperator.IN}
            and constraint.values
        ]
        all_constraints = [*transitions, *others]
        resolved, ambiguous = resolve_field_group(
            tables, [constraint.field for constraint in all_constraints]
        )
        for field, count in ambiguous:
            proposal.warnings.append(
                f"state_path_field_ambiguous:{field}:{count}"
            )
        resolved_by_constraint = {
            id(constraint): matches
            for constraint, matches in zip(all_constraints, resolved)
        }
        for constraint in transitions:
            matches = resolved_by_constraint[id(constraint)]
            if len(matches) != 1:
                continue
            table, column = matches[0]
            before = _transition_value(str(constraint.values[0]).strip(), column)
            after = _transition_value(str(constraint.values[1]).strip(), column)
            _add_edge(
                edges,
                _Edge(
                    table.name,
                    column.name,
                    str(before),
                    str(after),
                    before,
                    after,
                    rule.rule_key,
                    condition,
                ),
            )


def _collect_pair_edges(
    rule: Rule,
    tables: list[TableCatalog],
    edges: dict[tuple[str, str, str, str], _Edge],
    proposal: StateProposal,
) -> None:
    groups: dict[str, list[RuleConstraint]] = {}
    for constraint in rule.constraints:
        if constraint.operator not in {RuleOperator.EQ, RuleOperator.IN}:
            continue
        if len(constraint.values) != 1 or _pair_key(constraint.field) is None:
            continue
        groups.setdefault(constraint.condition or "", []).append(constraint)
    for condition in groups:
        all_constraints = [
            (constraint, [str(value).strip() for value in constraint.values])
            for constraint in rule.constraints
            if (constraint.condition or "") == condition
            and constraint.operator in {RuleOperator.EQ, RuleOperator.IN}
            and constraint.values
        ]
        resolved, ambiguous = resolve_field_group(
            tables, [constraint.field for constraint, _values in all_constraints]
        )
        for field, count in ambiguous:
            proposal.warnings.append(
                f"state_path_field_ambiguous:{field}:{count}"
            )
        resolved_by_constraint = {
            id(constraint): matches
            for (constraint, _values), matches in zip(all_constraints, resolved)
        }
        pairs: dict[tuple[str, str], dict[str, tuple[ColumnCatalog, str]]] = {}
        for constraint, _values in all_constraints:
            parsed = _pair_key(constraint.field)
            matches = resolved_by_constraint[id(constraint)]
            if parsed is None or len(matches) != 1:
                continue
            side, base = parsed
            table, column = matches[0]
            pairs.setdefault((table.name, base), {})[side] = (
                column,
                str(constraint.values[0]).strip(),
            )
        for (table_name, base), sides in pairs.items():
            if "before" not in sides or "after" not in sides:
                continue
            before_column, before_raw = sides["before"]
            after_column, after_raw = sides["after"]
            before = _transition_value(before_raw, before_column)
            after = _transition_value(after_raw, after_column)
            _add_edge(
                edges,
                _Edge(
                    table_name,
                    base,
                    str(before),
                    str(after),
                    before,
                    after,
                    rule.rule_key,
                    condition,
                ),
            )


def _add_edge(
    edges: dict[tuple[str, str, str, str], _Edge],
    edge: _Edge,
) -> None:
    edges.setdefault(
        (edge.table, edge.column, edge.before_key, edge.after_key), edge
    )


def _find_multi_step_paths(
    edges: list[_Edge],
    proposal: StateProposal,
) -> list[list[_Edge]]:
    adjacency: dict[str, list[_Edge]] = {}
    incoming: set[str] = set()
    for edge in edges:
        adjacency.setdefault(edge.before_key, []).append(edge)
        incoming.add(edge.after_key)
    starts = [edge for edge in edges if edge.before_key not in incoming]
    if not starts:
        starts = list(edges)
    paths: list[list[_Edge]] = []
    seen: set[tuple[tuple[str, str, str], ...]] = set()

    def signature(path: list[_Edge]) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (edge.rule_key, edge.before_key, edge.after_key) for edge in path
        )

    def walk(path: list[_Edge], used: set[tuple[str, str, str]]) -> None:
        if len(path) >= MAX_PATH_STEPS:
            if len(path) >= 2:
                key = signature(path)
                if key not in seen:
                    seen.add(key)
                    paths.append(list(path))
            return
        last = path[-1]
        candidates = [
            edge
            for edge in adjacency.get(last.after_key, [])
            if (edge.rule_key, edge.before_key, edge.after_key) not in used
        ]
        if not candidates:
            if len(path) >= 2:
                key = signature(path)
                if key not in seen:
                    seen.add(key)
                    paths.append(list(path))
            return
        for edge in candidates:
            marker = (edge.rule_key, edge.before_key, edge.after_key)
            used.add(marker)
            path.append(edge)
            walk(path, used)
            path.pop()
            used.remove(marker)

    for start in starts:
        marker = (start.rule_key, start.before_key, start.after_key)
        walk([start], {marker})
        if len(paths) >= MAX_STATE_PATHS:
            proposal.warnings.append(f"state_path_limit:{MAX_STATE_PATHS}")
            break
    return paths


def _state_path(
    table: str,
    column: str,
    edges: list[_Edge],
) -> StatePath:
    payload = {
        "table": table,
        "column": column,
        "steps": [
            {
                "rule_key": edge.rule_key,
                "condition": edge.condition,
                "from": edge.before_key,
                "to": edge.after_key,
            }
            for edge in edges
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:6]
    label = "".join(
        char if char.isalnum() else "_" for char in f"{table}_{column}"
    )
    return StatePath(
        path_key=f"path_{label}_{digest}",
        table=table,
        column=column,
        steps=[
            StatePathStep(
                rule_key=edge.rule_key,
                condition=edge.condition,
                from_value=edge.before_value,
                to_value=edge.after_value,
            )
            for edge in edges
        ],
        source="rule",
        evidence=Evidence.INFERRED,
    )


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
                parent_table = table_index[fk.parent_table]
                parent_spec = parent_table.column_index().get(parent_column)
                if parent_spec is None:
                    raise _StateError(
                        f"fk_parent_column_missing:"
                        f"{fk.parent_table}.{parent_column}"
                    )
                value, reason = _placeholder(parent_spec)
                if value is None:
                    raise _StateError(
                        f"fk_parent_value_unsupported:"
                        f"{fk.parent_table}.{parent_column}"
                    )
                parent_row[parent_column] = value
                placeholders.append(
                    (fk.parent_table, parent_column, reason)
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
