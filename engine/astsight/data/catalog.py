"""C5a 数据目录：DDL + seed → 结构目录、FK 图、拓扑顺序、候选业务态。

本模块确定性、不调用模型；字段业务语义的补全由适配层完成后再合并。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from contracts import (
    BusinessState,
    ColumnCatalog,
    ColumnSemantic,
    DataCatalog,
    DataState,
    DataWriteMode,
    Evidence,
    FieldSemantics,
    ForeignKey,
    TableCatalog,
    ValueSemantics,
)

from .ddl import ParsedColumn, ParsedDDL, ParsedTable
from .seed import SeedData

CATALOG_SCHEMA_VERSION = 1
MAX_TABLES = 500


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _words(name: str) -> set[str]:
    cleaned = name.lower().replace("-", "_")
    parts = {part for part in cleaned.split("_") if part}
    return parts


def _semantic_for(column: ParsedColumn, *, is_fk: bool) -> ColumnSemantic:
    name = column.name.lower()
    words = _words(name)
    if column.enum_values:
        return ColumnSemantic.ENUM
    if is_fk or (name.endswith("_id") and name != "id"):
        return ColumnSemantic.REFERENCE
    if name in {"id", "uuid", "guid"} or column.primary_key:
        return ColumnSemantic.IDENTIFIER
    if words & {"status", "state", "stage", "phase"}:
        return ColumnSemantic.STATUS
    if words & {"amount", "price", "cost", "fee", "balance", "total"}:
        return ColumnSemantic.AMOUNT
    if words & {"quantity", "qty", "count", "stock", "num", "number", "score"}:
        return ColumnSemantic.QUANTITY
    if words & {"created", "updated", "deleted", "time", "timestamp", "date", "at"}:
        if any(token in column.type.upper() for token in ("TIME", "DATE")):
            return ColumnSemantic.TIMESTAMP
    if name.startswith(("is_", "has_", "can_", "allow_", "enabled")):
        return ColumnSemantic.BOOLEAN
    if words & {"type", "kind", "category", "channel", "code", "level"}:
        return ColumnSemantic.ENUM
    if any(token in column.type.upper() for token in ("CHAR", "TEXT", "CLOB")):
        return ColumnSemantic.TEXT
    return ColumnSemantic.UNKNOWN


def _value_semantics(column: ParsedColumn) -> list[ValueSemantics]:
    out: list[ValueSemantics] = []
    for value in column.enum_values:
        out.append(
            ValueSemantics(
                value=value,
                meaning=str(value),
                evidence=Evidence.INFERRED,
            )
        )
    return out


def _fk_columns(ddl: ParsedDDL) -> set[tuple[str, str]]:
    return {
        (fk.child_table, column)
        for fk in ddl.foreign_keys
        for column in fk.child_columns
    }


def _table_catalog(
    table: ParsedTable, *, fk_columns: set[tuple[str, str]]
) -> TableCatalog:
    columns = [
        ColumnCatalog(
            name=column.name,
            type=column.type,
            nullable=column.nullable,
            default=column.default,
            primary_key=column.primary_key or column.name in table.primary_key,
            unique=column.unique,
            comment=column.comment,
            enum_values=list(column.enum_values),
            semantic_type=_semantic_for(
                column, is_fk=(table.name, column.name) in fk_columns
            ),
            value_semantics=_value_semantics(column),
            evidence=Evidence.INFERRED,
        )
        for column in table.columns
    ]
    return TableCatalog(
        name=table.name,
        columns=columns,
        primary_key=list(table.primary_key),
        foreign_keys=[
            ForeignKey(
                child_table=fk.child_table,
                child_columns=list(fk.child_columns),
                parent_table=fk.parent_table,
                parent_columns=list(fk.parent_columns),
                evidence=Evidence.INFERRED,
            )
            for fk in table.foreign_keys
        ],
        comment=table.comment,
        read_write=DataWriteMode.BOTH,
        evidence=Evidence.INFERRED,
    )


def _topological_order(
    tables: dict[str, TableCatalog],
    foreign_keys: list[ForeignKey],
    warnings: list[str],
) -> list[str]:
    edges: list[tuple[str, str]] = []
    for fk in foreign_keys:
        if fk.parent_table not in tables or fk.child_table not in tables:
            warnings.append(
                f"fk_unknown_table:{fk.child_table}->{fk.parent_table}"
            )
            continue
        if fk.parent_table != fk.child_table:
            edges.append((fk.parent_table, fk.child_table))

    def order(edges_in: Iterable[tuple[str, str]], nodes: list[str]) -> tuple[list[str], list[str]]:
        outgoing: dict[str, set[str]] = {name: set() for name in nodes}
        indegree: dict[str, int] = {name: 0 for name in nodes}
        for parent, child in edges_in:
            if child not in outgoing[parent]:
                outgoing[parent].add(child)
                indegree[child] += 1
        ready = sorted(name for name in nodes if indegree[name] == 0)
        out: list[str] = []
        while ready:
            node = ready.pop(0)
            out.append(node)
            for child in sorted(outgoing[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        remaining = [name for name in nodes if name not in out]
        return out, remaining

    nodes = sorted(tables)
    out, remaining = order(edges, nodes)
    while remaining:
        # 断最弱可空边；没有可空边时断名字最小的一条，并显式告警。
        candidates = [
            (parent, child)
            for parent, child in edges
            if child in remaining
        ]
        chosen: tuple[str, str] | None = None
        for parent, child in sorted(candidates):
            fk_list = [
                fk
                for fk in foreign_keys
                if fk.child_table == child and fk.parent_table == parent
            ]
            if fk_list and all(
                all(
                    tables[child].column_index().get(column) is not None
                    and tables[child].column_index()[column].nullable
                    for column in fk.child_columns
                )
                for fk in fk_list
            ):
                chosen = (parent, child)
                break
        if chosen is None and candidates:
            chosen = sorted(candidates)[0]
        if chosen is None:
            out.extend(sorted(remaining))
            break
        edges = [edge for edge in edges if edge != chosen]
        warnings.append(f"fk_cycle_broken:{chosen[1]}->{chosen[0]}")
        out, remaining = order(edges, nodes)
    return out


def _seed_states(seed: SeedData) -> list[DataState]:
    states: list[DataState] = []
    for table in sorted(seed.tables):
        rows = seed.tables.get(table) or []
        if not rows:
            continue
        states.append(
            DataState(
                state_key=f"seed_{table}",
                target_state={},
                table_values={table: [dict(row) for row in rows]},
                evidence=Evidence.INFERRED,
            )
        )
    return states


def build_catalog(
    *,
    project: str,
    ddl: ParsedDDL,
    seed: SeedData | None = None,
    context_id: str | None = None,
    dialect: str = "sqlite",
    sources: dict[str, Any] | None = None,
    field_semantics: list[FieldSemantics] | None = None,
    states: list[DataState] | None = None,
    gaps: list[dict[str, Any]] | None = None,
    questions: list[dict[str, Any]] | None = None,
) -> DataCatalog:
    warnings = list(ddl.warnings)
    seed = seed or SeedData()
    fk_columns = _fk_columns(ddl)
    tables = [
        _table_catalog(table, fk_columns=fk_columns) for table in ddl.tables
    ]
    if len(tables) > MAX_TABLES:
        raise ValueError(f"表数量超过上限 {MAX_TABLES}")
    table_index = {table.name: table for table in tables}
    foreign_keys = [
        fk for table in tables for fk in table.foreign_keys
    ]
    order = _topological_order(table_index, foreign_keys, warnings)

    gaps_out = list(gaps or [])
    for table in tables:
        if not table.primary_key:
            gaps_out.append(
                {
                    "kind": "no_primary_key",
                    "table": table.name,
                    "reason": "无法生成可回收的 teardown；需人工确认主键",
                }
            )
        for column in table.columns:
            if column.semantic_type is ColumnSemantic.UNKNOWN:
                gaps_out.append(
                    {
                        "kind": "field_semantics_unmapped",
                        "table": table.name,
                        "column": column.name,
                        "reason": "命名/类型无法确定业务语义，需模型或人工补全",
                    }
                )
    known_fks = {(fk.child_table, column) for fk in foreign_keys for column in fk.child_columns}
    for table in tables:
        for column in table.columns:
            if (
                column.name.lower().endswith("_id")
                and column.name.lower() != "id"
                and (table.name, column.name) not in known_fks
            ):
                gaps_out.append(
                    {
                        "kind": "fk_unmapped",
                        "table": table.name,
                        "column": column.name,
                        "reason": "疑似外键但缺少 REFERENCES 约束",
                    }
                )

    states_out: list[DataState] = []
    seen_states: set[str] = set()
    for state in [*(states or []), *_seed_states(seed)]:
        if state.state_key in seen_states:
            continue
        seen_states.add(state.state_key)
        states_out.append(state)
    sources_out = dict(sources or {})
    catalog_payload = {
        "tables": [table.model_dump(mode="json") for table in tables],
        "foreign_keys": [fk.model_dump(mode="json") for fk in foreign_keys],
        "order": order,
        "seed": seed.tables,
    }
    return DataCatalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        catalog_id="dc-" + _sha(catalog_payload)[:16],
        project=project,
        context_id=context_id,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dialect=dialect,
        sources=sources_out,
        tables=tables,
        foreign_keys=foreign_keys,
        tables_order=order,
        field_semantics=list(field_semantics or []),
        states=states_out,
        seeds=[
            {"table": table, "rows": len(rows)}
            for table, rows in sorted(seed.tables.items())
        ],
        limits={
            "max_tables": MAX_TABLES,
            "max_rows_per_table": 1_000,
            "max_columns_per_table": 500,
        },
        gaps=gaps_out,
        questions=list(questions or []),
        warnings=warnings,
    )


__all__ = ["CATALOG_SCHEMA_VERSION", "MAX_TABLES", "build_catalog"]
