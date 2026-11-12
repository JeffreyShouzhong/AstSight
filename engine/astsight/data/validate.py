"""数据目录/ fixture 的确定性校验。"""
from __future__ import annotations

from typing import Any

from contracts import DataCatalog, ForeignKey, TableCatalog

from .dialects import DialectError, normalize_dialect

MAX_FIXTURE_ROWS_PER_TABLE = 1_000
MAX_FIXTURE_TOTAL_ROWS = 10_000


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def check_type(column_type: str, value: Any) -> bool:
    if value is None:
        return True
    upper = (column_type or "").upper()
    if any(token in upper for token in ("INT", "SERIAL")):
        return isinstance(value, int) and not isinstance(value, bool)
    if any(token in upper for token in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if "BOOL" in upper:
        return isinstance(value, (bool, int))
    if any(token in upper for token in ("CHAR", "TEXT", "CLOB", "ENUM", "UUID")):
        return isinstance(value, str)
    return True


def validate_catalog(catalog: DataCatalog) -> list[str]:
    errors: list[str] = []
    if catalog.schema_version != 1:
        errors.append(f"catalog_schema_unsupported:{catalog.schema_version}")
    try:
        normalize_dialect(catalog.dialect)
    except DialectError:
        errors.append(f"catalog_dialect_unsupported:{catalog.dialect}")
    if not catalog.catalog_id:
        errors.append("catalog_id_empty")
    tables = catalog.table_index()
    if len(tables) != len(catalog.tables):
        errors.append("table_duplicate")
    for table in catalog.tables:
        columns = table.column_index()
        if len(columns) != len(table.columns):
            errors.append(f"column_duplicate:{table.name}")
        for name in table.primary_key:
            if name not in columns:
                errors.append(f"pk_column_unknown:{table.name}.{name}")
        for column in table.columns:
            if not _is_scalar(column.default):
                errors.append(f"default_not_scalar:{table.name}.{column.name}")
            for value in column.enum_values:
                if not _is_scalar(value):
                    errors.append(f"enum_not_scalar:{table.name}.{column.name}")
            for value in column.value_semantics:
                if not _is_scalar(value.value):
                    errors.append(
                        f"value_semantics_not_scalar:{table.name}.{column.name}"
                    )
    for fk in catalog.foreign_keys:
        if fk.child_table not in tables:
            errors.append(f"fk_child_unknown:{fk.child_table}")
            continue
        if fk.parent_table not in tables:
            errors.append(f"fk_parent_unknown:{fk.parent_table}")
            continue
        child_columns = tables[fk.child_table].column_index()
        parent_columns = tables[fk.parent_table].column_index()
        for name in fk.child_columns:
            if name not in child_columns:
                errors.append(f"fk_child_column_unknown:{fk.child_table}.{name}")
        for name in fk.parent_columns:
            if name not in parent_columns:
                errors.append(f"fk_parent_column_unknown:{fk.parent_table}.{name}")
        if len(fk.child_columns) != len(fk.parent_columns):
            errors.append(
                f"fk_column_count_mismatch:{fk.child_table}->{fk.parent_table}"
            )
    if len(set(catalog.tables_order)) != len(catalog.tables_order):
        errors.append("tables_order_duplicate")
    missing_order = sorted(set(tables) - set(catalog.tables_order))
    if missing_order:
        errors.append(f"tables_order_missing:{missing_order}")
    extra_order = sorted(set(catalog.tables_order) - set(tables))
    if extra_order:
        errors.append(f"tables_order_unknown:{extra_order}")
    states: set[str] = set()
    for state in catalog.states:
        if state.state_key in states:
            errors.append(f"state_duplicate:{state.state_key}")
        states.add(state.state_key)
        for table, rows in state.table_values.items():
            if table not in tables:
                errors.append(f"state_table_unknown:{state.state_key}.{table}")
                continue
            columns = tables[table].column_index()
            for row in rows:
                for column in row:
                    if column not in columns:
                        errors.append(
                            f"state_column_unknown:{state.state_key}.{table}.{column}"
                        )
    for semantics in catalog.field_semantics:
        table = tables.get(semantics.table)
        if table is None:
            errors.append(f"semantics_table_unknown:{semantics.table}")
            continue
        if semantics.column not in table.column_index():
            errors.append(
                f"semantics_column_unknown:{semantics.table}.{semantics.column}"
            )
    return errors


def validate_fixtures(
    catalog: DataCatalog,
    fixtures: dict[str, list[dict[str, Any]]],
    *,
    max_rows_per_table: int = MAX_FIXTURE_ROWS_PER_TABLE,
) -> list[str]:
    errors: list[str] = []
    tables = catalog.table_index()
    total = 0
    for table_name, rows in fixtures.items():
        table = tables.get(table_name)
        if table is None:
            errors.append(f"fixture_table_unknown:{table_name}")
            continue
        if not isinstance(rows, list):
            errors.append(f"fixture_rows_not_list:{table_name}")
            continue
        if len(rows) > max_rows_per_table:
            errors.append(
                f"fixture_rows_limit:{table_name}:{len(rows)}>{max_rows_per_table}"
            )
        total += len(rows)
        columns = table.column_index()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"fixture_row_not_object:{table_name}[{index}]")
                continue
            for name, value in row.items():
                column = columns.get(name)
                if column is None:
                    errors.append(
                        f"fixture_column_unknown:{table_name}[{index}].{name}"
                    )
                    continue
                if not _is_scalar(value):
                    errors.append(
                        f"fixture_value_not_scalar:{table_name}[{index}].{name}"
                    )
                elif value is None and not column.nullable:
                    errors.append(
                        f"fixture_not_null:{table_name}[{index}].{name}"
                    )
                elif not check_type(column.type, value):
                    errors.append(
                        f"fixture_type:{table_name}[{index}].{name}:{column.type}"
                    )
                elif column.enum_values and value not in column.enum_values:
                    errors.append(
                        f"fixture_enum:{table_name}[{index}].{name}:{value!r}"
                    )
    if total > MAX_FIXTURE_TOTAL_ROWS:
        errors.append(f"fixture_total_rows_limit:{total}>{MAX_FIXTURE_TOTAL_ROWS}")
    return errors


def _fk_values(fk: ForeignKey, row: dict[str, Any]) -> tuple[Any, ...] | None:
    values: list[Any] = []
    for name in fk.child_columns:
        if name not in row or row.get(name) is None:
            return None
        values.append(row.get(name))
    return tuple(values)


def validate_fk_closure(
    catalog: DataCatalog,
    fixtures: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """fixture 中的非空外键必须有同批 parent 行；不假设库里已有数据。"""
    errors: list[str] = []
    for fk in catalog.foreign_keys:
        child_rows = fixtures.get(fk.child_table) or []
        parent_rows = fixtures.get(fk.parent_table) or []
        parent_keys = {
            tuple(row.get(name) for name in fk.parent_columns)
            for row in parent_rows
        }
        missing: set[tuple[Any, ...]] = set()
        for row in child_rows:
            values = _fk_values(fk, row)
            if values is None:
                continue
            if values not in parent_keys:
                missing.add(values)
        for values in sorted(missing, key=str):
            errors.append(
                f"fk_parent_missing:{fk.child_table}->{fk.parent_table}:{values}"
            )
    return errors


def validate_plan_fixtures(
    catalog: DataCatalog,
    fixtures: dict[str, list[dict[str, Any]]],
) -> list[str]:
    return [*validate_fixtures(catalog, fixtures), *validate_fk_closure(catalog, fixtures)]


__all__ = [
    "MAX_FIXTURE_ROWS_PER_TABLE",
    "MAX_FIXTURE_TOTAL_ROWS",
    "check_type",
    "validate_catalog",
    "validate_fixtures",
    "validate_fk_closure",
    "validate_plan_fixtures",
]
