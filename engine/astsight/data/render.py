"""把通过校验的 fixtures 渲染成参数化 SQL。

模型永远不输出 SQL：标识符来自 catalog 且必须匹配白名单；值只通过 params 传递。
"""
from __future__ import annotations

import re
from typing import Any

from contracts import DataCatalog, DataPlan

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
MAX_STATEMENTS = 10_000


class RenderError(ValueError):
    """标识符不安全或无法生成可回收 SQL。"""


def quote_identifier(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise RenderError(f"非法标识符: {name!r}")
    return f'"{name}"'


def _sql_value(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def render_inserts(
    catalog: DataCatalog,
    fixtures: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    tables = catalog.table_index()
    statements: list[dict[str, Any]] = []
    for table in catalog.tables_order:
        rows = fixtures.get(table) or []
        spec = tables[table]
        columns = spec.column_index()
        for row in rows:
            names = [name for name in columns if name in row]
            if not names:
                continue
            sql = (
                f"INSERT INTO {quote_identifier(table)} "
                f"({', '.join(quote_identifier(name) for name in names)}) "
                f"VALUES ({', '.join('?' for _ in names)})"
            )
            statements.append(
                {
                    "kind": "insert",
                    "table": table,
                    "sql": sql,
                    "params": [_sql_value(row[name]) for name in names],
                }
            )
    if len(statements) > MAX_STATEMENTS:
        raise RenderError(f"SQL 语句数 {len(statements)} 超过上限 {MAX_STATEMENTS}")
    return statements


def render_teardown(
    catalog: DataCatalog,
    fixtures: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    tables = catalog.table_index()
    statements: list[dict[str, Any]] = []
    # 逆拓扑顺序删除，先删子表。
    for table in reversed(catalog.tables_order):
        rows = fixtures.get(table) or []
        if not rows:
            continue
        spec = tables[table]
        pk = list(spec.primary_key)
        if not pk:
            warnings.append(f"teardown_no_primary_key:{table}")
            continue
        for row in rows:
            if any(name not in row or row.get(name) is None for name in pk):
                warnings.append(f"teardown_pk_missing:{table}")
                continue
            where = " AND ".join(f"{quote_identifier(name)} = ?" for name in pk)
            statements.append(
                {
                    "kind": "delete",
                    "table": table,
                    "sql": f"DELETE FROM {quote_identifier(table)} WHERE {where}",
                    "params": [_sql_value(row[name]) for name in pk],
                }
            )
    if len(statements) > MAX_STATEMENTS:
        raise RenderError(f"teardown 语句数 {len(statements)} 超过上限 {MAX_STATEMENTS}")
    return statements, warnings


def render_plan(plan: DataPlan, catalog: DataCatalog) -> DataPlan:
    if plan.tables_order and plan.tables_order != catalog.tables_order:
        unknown = sorted(set(plan.tables_order) - set(catalog.tables_order))
        if unknown:
            raise RenderError(f"plan.tables_order 含未知表: {unknown}")
    inserts = render_inserts(catalog, plan.fixtures)
    deletes, warnings = render_teardown(catalog, plan.fixtures)
    plan.statements = inserts
    plan.warnings = list(dict.fromkeys([*plan.warnings, *warnings]))
    plan.teardown = deletes
    return plan


__all__ = [
    "MAX_STATEMENTS",
    "RenderError",
    "quote_identifier",
    "render_inserts",
    "render_plan",
    "render_teardown",
]
