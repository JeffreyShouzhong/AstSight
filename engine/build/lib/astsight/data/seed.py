"""种子数据（seed）读取与校验。

seed 是可选事实源：只允许引用 DDL 中存在的表/列；值必须是标量或 null。
v1 不接受自由 SQL，也不接受未知字段。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ddl import ParsedDDL

MAX_SEED_BYTES = 10 * 1024 * 1024
MAX_ROWS_PER_TABLE = 1_000
MAX_TOTAL_ROWS = 10_000
MAX_COLUMNS_PER_ROW = 200


class SeedError(ValueError):
    """seed 不可加载或引用未知表/列。"""


@dataclass
class SeedData:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def rows(self, table: str) -> list[dict[str, Any]]:
        return list(self.tables.get(table) or [])

    @property
    def total_rows(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _check_type(column_type: str, value: Any) -> bool:
    if value is None:
        return True
    upper = column_type.upper()
    if any(token in upper for token in ("INT", "SERIAL")):
        return isinstance(value, int) and not isinstance(value, bool)
    if any(token in upper for token in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if "BOOL" in upper:
        return isinstance(value, (bool, int))
    if any(token in upper for token in ("CHAR", "TEXT", "CLOB", "ENUM", "UUID")):
        return isinstance(value, str)
    return True


def _normalize_root(data: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(data, dict):
        raise SeedError("seed 根节点必须是 JSON object")
    raw_tables = data.get("tables", data)
    if not isinstance(raw_tables, dict):
        raise SeedError("seed.tables 必须是 table -> rows 的 object")
    normalized: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for table, rows in raw_tables.items():
        if table in {"schema_version", "project", "generated_at", "notes"}:
            continue
        if not isinstance(rows, list):
            raise SeedError(f"seed.{table} 必须是行数组")
        if len(rows) > MAX_ROWS_PER_TABLE:
            raise SeedError(f"seed.{table} 行数 {len(rows)} 超过上限 {MAX_ROWS_PER_TABLE}")
        total += len(rows)
        if total > MAX_TOTAL_ROWS:
            raise SeedError(f"seed 总行数超过上限 {MAX_TOTAL_ROWS}")
        out_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise SeedError(f"seed.{table}[{index}] 必须是 object")
            if len(row) > MAX_COLUMNS_PER_ROW:
                raise SeedError(f"seed.{table}[{index}] 列数超过上限")
            for key, value in row.items():
                if not _is_scalar(value):
                    raise SeedError(f"seed.{table}[{index}].{key} 必须是标量或 null")
            out_rows.append(dict(row))
        normalized[str(table)] = out_rows
    return normalized


def load_seed(
    path: str | Path | None,
    ddl: ParsedDDL | None = None,
) -> SeedData:
    if path is None:
        return SeedData()
    seed_path = Path(path)
    if not seed_path.is_file():
        raise SeedError(f"seed 文件不存在: {seed_path}")
    if seed_path.stat().st_size > MAX_SEED_BYTES:
        raise SeedError(f"seed 过大: {seed_path.stat().st_size} bytes")
    try:
        data = json.loads(seed_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError(f"seed 不是合法 JSON: {exc}") from exc
    tables = _normalize_root(data)
    result = SeedData(tables=tables)
    if ddl is None:
        return result
    table_index = ddl.table_index()
    for table, rows in tables.items():
        parsed = table_index.get(table)
        if parsed is None:
            raise SeedError(f"seed 引用了 DDL 中不存在的表: {table}")
        columns = parsed.column_index()
        for index, row in enumerate(rows):
            for column, value in row.items():
                spec = columns.get(column)
                if spec is None:
                    raise SeedError(
                        f"seed.{table}[{index}] 引用了不存在的列: {column}"
                    )
                if not spec.nullable and value is None:
                    raise SeedError(f"seed.{table}[{index}].{column} 不可为 null")
                if not _check_type(spec.type, value):
                    raise SeedError(
                        f"seed.{table}[{index}].{column} 类型不符: "
                        f"期望 {spec.type}, 实际 {type(value).__name__}"
                    )
                if spec.enum_values and value not in spec.enum_values:
                    raise SeedError(
                        f"seed.{table}[{index}].{column} 不在枚举中: {value!r}"
                    )
    return result


__all__ = [
    "MAX_ROWS_PER_TABLE",
    "MAX_SEED_BYTES",
    "MAX_TOTAL_ROWS",
    "SeedData",
    "SeedError",
    "load_seed",
]
