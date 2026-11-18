"""多行 fixture 的关系解析：跨行 `$ref` 与父子外键关联。"""
from __future__ import annotations

import copy
import re
from typing import Any

from contracts import DataCatalog, ForeignKey

MAX_REFERENCE_DEPTH = 8
_REF_RE = re.compile(
    r"^(?P<table>[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)"
    r"\[(?P<index>\d+)\]\.(?P<column>[A-Za-z_][A-Za-z0-9_$]*)$"
)


def prepare_plan_fixtures(
    catalog: DataCatalog,
    fixtures: dict[str, Any],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    list[str],
    list[str],
]:
    """复制 fixtures、解析 `$ref` 并补全可唯一推导的父子外键。"""
    normalized: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for table, rows in fixtures.items():
        if not isinstance(rows, list):
            errors.append(f"fixture_rows_not_list:{table}")
            continue
        normalized[str(table)] = [
            copy.deepcopy(row) if isinstance(row, dict) else row
            for row in rows
        ]
    if errors:
        return normalized, errors, warnings

    for table, rows in normalized.items():
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for column, value in list(row.items()):
                resolved, error = _resolve_value(
                    value,
                    normalized,
                    stack=set(),
                )
                if error:
                    errors.append(
                        f"fixture_ref_invalid:{table}[{index}].{column}:{error}"
                    )
                else:
                    row[column] = resolved
    if errors:
        return normalized, errors, warnings

    tables = catalog.table_index()
    for fk in catalog.foreign_keys:
        _fill_parent_links(
            fk,
            normalized,
            tables,
            errors,
            warnings,
        )
    return normalized, sorted(set(errors)), sorted(set(warnings))


def _resolve_value(
    value: Any,
    fixtures: dict[str, list[dict[str, Any]]],
    *,
    stack: set[tuple[str, int, str]],
) -> tuple[Any, str | None]:
    if not (
        isinstance(value, dict)
        and set(value) == {"$ref"}
        and isinstance(value.get("$ref"), str)
    ):
        return value, None
    raw = value["$ref"].strip()
    match = _REF_RE.fullmatch(raw)
    if match is None:
        return None, "expected table[index].column"
    table = match.group("table")
    index = int(match.group("index"))
    column = match.group("column")
    marker = (table, index, column)
    if marker in stack:
        return None, "cycle"
    if len(stack) >= MAX_REFERENCE_DEPTH:
        return None, "depth_limit"
    rows = fixtures.get(table)
    if rows is None:
        return None, f"table_unknown:{table}"
    if index >= len(rows) or not isinstance(rows[index], dict):
        return None, f"row_unknown:{table}[{index}]"
    if column not in rows[index]:
        return None, f"column_unknown:{table}[{index}].{column}"
    return _resolve_value(
        rows[index][column],
        fixtures,
        stack={*stack, marker},
    )


def _fill_parent_links(
    fk: ForeignKey,
    fixtures: dict[str, list[dict[str, Any]]],
    tables: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    child_rows = [
        row
        for row in fixtures.get(fk.child_table) or []
        if isinstance(row, dict)
    ]
    parent_rows = [
        row
        for row in fixtures.get(fk.parent_table) or []
        if isinstance(row, dict)
    ]
    if not child_rows or not parent_rows:
        return
    parent_keys = {
        tuple(row.get(name) for name in fk.parent_columns)
        for row in parent_rows
    }
    parent_keys = {
        key
        for key in parent_keys
        if all(value is not None for value in key)
    }
    if not parent_keys:
        return
    child_table = tables.get(fk.child_table)
    if child_table is None:
        return
    child_columns = child_table.column_index()

    missing_rows: list[dict[str, Any]] = []
    for row in child_rows:
        values = [row.get(name) for name in fk.child_columns]
        if any(value is None for value in values):
            missing_rows.append(row)
            continue
        if tuple(values) not in parent_keys:
            errors.append(
                f"fk_parent_missing:{fk.child_table}->{fk.parent_table}:"
                f"{tuple(values)}"
            )
    if not missing_rows:
        return

    if len(parent_keys) == 1:
        parent_key = next(iter(parent_keys))
        for row in missing_rows:
            for column, value in zip(
                fk.child_columns,
                parent_key,
                strict=True,
            ):
                current = row.get(column)
                if current is not None and current != value:
                    errors.append(
                        f"fixture_fk_conflict:{fk.child_table}.{column}"
                    )
                    continue
                row[column] = value
        warnings.append(
            f"fixture_fk_autofilled:{fk.child_table}->{fk.parent_table}:"
            f"rows={len(missing_rows)}"
        )
        return

    has_required_missing = any(
        not child_columns[name].nullable
        for row in missing_rows
        for name in fk.child_columns
        if row.get(name) is None
        and name in child_columns
    )
    if has_required_missing:
        errors.append(
            f"fixture_fk_ambiguous:{fk.child_table}->{fk.parent_table}:"
            f"parents={len(parent_keys)}"
        )


__all__ = ["MAX_REFERENCE_DEPTH", "prepare_plan_fixtures"]
