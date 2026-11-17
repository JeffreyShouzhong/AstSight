"""从真实数据库反射表结构，产出与 DDL 解析相同的 ``ParsedDDL``。

支持 SQLite / PostgreSQL / MySQL。只读 information_schema / pg_catalog /
sqlite_master，不读取业务数据。方言差异留在本模块，catalog 构建继续复用
``build_catalog``，因此反射、DDL、DBML 三条入口的产物完全同构。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Iterable

from .connection import DbConnectError, connect
from .ddl import (
    MAX_TABLES,
    ParsedColumn,
    ParsedDDL,
    ParsedForeignKey,
    ParsedTable,
    normalize_type,
    parse_ddl,
    parse_enum_values,
)
from .dialects import DbTarget, parse_db_url

class ReflectError(RuntimeError):
    """数据库不可连接、结构不可读或超出资源上限。"""


@dataclass
class Reflection:
    ddl: ParsedDDL
    dialect: str
    schemas: list[str] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def reflect_database(
    db_url: str,
    *,
    schemas: Iterable[str] | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> Reflection:
    """反射数据库结构；``schemas`` 为空时使用连接的当前 schema/database。"""
    try:
        target = parse_db_url(db_url)
    except Exception as exc:
        raise ReflectError(str(exc)) from exc
    schema_list = [item for item in (schemas or []) if item]
    include_list = [item for item in (include or []) if item]
    exclude_list = [item for item in (exclude or []) if item]
    if target.dialect == "sqlite":
        reflection = _reflect_sqlite(target)
    elif target.dialect == "postgres":
        reflection = _reflect_postgres(target, schema_list)
    else:
        reflection = _reflect_mysql(target, schema_list)
    _apply_filters(
        reflection.ddl, include_list, exclude_list, reflection.ddl.warnings
    )
    reflection.warnings = reflection.ddl.warnings
    if not reflection.ddl.tables:
        raise ReflectError(
            "reflection_empty: 没有反射到任何表；请检查 schema/过滤条件"
        )
    return reflection


def _apply_filters(
    ddl: ParsedDDL,
    include: list[str],
    exclude: list[str],
    warnings: list[str],
) -> None:
    if not include and not exclude:
        return

    def matched(name: str) -> bool:
        short = name.rsplit(".", 1)[-1]
        return any(
            fnmatch.fnmatchcase(name, pattern)
            or fnmatch.fnmatchcase(short, pattern)
            for pattern in include
        )

    kept: list[ParsedTable] = []
    removed: set[str] = set()
    for table in ddl.tables:
        short = table.name.rsplit(".", 1)[-1]
        if include and not matched(table.name):
            removed.add(table.name)
            continue
        if exclude and any(
            fnmatch.fnmatchcase(table.name, pattern)
            or fnmatch.fnmatchcase(short, pattern)
            for pattern in exclude
        ):
            removed.add(table.name)
            continue
        kept.append(table)
    for table in kept:
        table.foreign_keys = [
            fk for fk in table.foreign_keys if fk.parent_table not in removed
        ]
    if removed:
        warnings.append(f"reflection_filtered:{len(removed)}")
    ddl.tables = kept


# ───────────────────────────── SQLite ─────────────────────────────


def _reflect_sqlite(target: DbTarget) -> Reflection:
    conn = _connect(target)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if not rows:
            raise ReflectError("reflection_empty: SQLite 数据库没有表")
        ddl = parse_ddl(
            "\n;\n".join(str(row[1]) for row in rows if row[1]),
            dialect="sqlite",
        )
        tables = ddl.table_index()
        for name in sorted(tables):
            _sqlite_foreign_keys(conn, tables[name], ddl.warnings)
            _sqlite_unique_indexes(conn, tables[name], ddl.warnings)
    finally:
        conn.close()
    return Reflection(
        ddl=ddl,
        dialect="sqlite",
        schemas=[],
        sources={
            "kind": "reflection",
            "dialect": "sqlite",
            "display": target.display,
        },
    )


def _sqlite_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_primary_key(conn, table: str) -> list[str]:
    try:
        rows = conn.execute(
            f"PRAGMA table_info({_sqlite_ident(table)})"
        ).fetchall()
    except Exception:
        return []
    pk = sorted(
        ((int(row[5]), str(row[1])) for row in rows if row[5]),
        key=lambda item: item[0],
    )
    return [name for _order, name in pk]


def _sqlite_foreign_keys(
    conn, table: ParsedTable, warnings: list[str]
) -> None:
    existing = {
        (tuple(fk.child_columns), fk.parent_table, tuple(fk.parent_columns))
        for fk in table.foreign_keys
    }
    try:
        rows = conn.execute(
            f"PRAGMA foreign_key_list({_sqlite_ident(table.name)})"
        ).fetchall()
    except Exception as exc:
        warnings.append(f"sqlite_pragma_failed:{table.name}:{exc}")
        return
    groups: dict[Any, list[Any]] = {}
    for row in rows:
        groups.setdefault(row[0], []).append(row)
    pk_cache: dict[str, list[str]] = {}
    for items in groups.values():
        items.sort(key=lambda row: row[1])
        parent_table = str(items[0][2])
        child_columns = [str(row[3]) for row in items]
        parent_columns = [str(row[4]) if row[4] else "" for row in items]
        if any(not name for name in parent_columns):
            # ``REFERENCES parent`` 省略列名时，目标列是父表主键。
            primary_key = pk_cache.get(parent_table)
            if primary_key is None:
                primary_key = _sqlite_primary_key(conn, parent_table)
                pk_cache[parent_table] = primary_key
            if len(primary_key) != len(parent_columns):
                warnings.append(
                    f"sqlite_fk_parent_key_unresolved:"
                    f"{table.name}->{parent_table}"
                )
                continue
            parent_columns = list(primary_key)
        key = (tuple(child_columns), parent_table, tuple(parent_columns))
        if key in existing:
            continue
        existing.add(key)
        table.foreign_keys.append(
            ParsedForeignKey(
                child_table=table.name,
                child_columns=child_columns,
                parent_table=parent_table,
                parent_columns=parent_columns,
            )
        )


def _sqlite_unique_indexes(
    conn, table: ParsedTable, warnings: list[str]
) -> None:
    columns = table.column_index()
    try:
        rows = conn.execute(
            f"PRAGMA index_list({_sqlite_ident(table.name)})"
        ).fetchall()
    except Exception as exc:
        warnings.append(f"sqlite_pragma_failed:{table.name}:{exc}")
        return
    for row in rows:
        if len(row) < 4 or not row[2]:
            continue
        if str(row[3]) == "pk":
            continue
        index_name = str(row[1])
        try:
            info = conn.execute(
                f"PRAGMA index_info({_sqlite_ident(index_name)})"
            ).fetchall()
        except Exception as exc:
            warnings.append(f"sqlite_pragma_failed:{index_name}:{exc}")
            continue
        names = [str(item[2]) for item in info]
        if len(names) == 1 and names[0] in columns:
            columns[names[0]].unique = True
        elif len(names) > 1:
            warnings.append(
                f"unique_composite_ignored:{table.name}:{','.join(names)}"
            )


# ──────────────────────────── PostgreSQL ────────────────────────────

_PG_TABLES = """
SELECT n.nspname, c.relname, obj_description(c.oid, 'pg_class')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s)
ORDER BY n.nspname, c.relname
"""

_PG_COLUMNS = """
SELECT n.nspname, c.relname, a.attname,
       format_type(a.atttypid, a.atttypmod) AS type,
       a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr,
       col_description(c.oid, a.attnum) AS comment,
       a.atttypid
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p')
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum
"""

_PG_ENUMS = """
SELECT e.enumtypid, e.enumlabel
FROM pg_enum e
ORDER BY e.enumtypid, e.enumsortorder
"""

_PG_UNIQUE_INDEXES = """
SELECT n.nspname, c.relname,
       ARRAY(
           SELECT a.attname
           FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute a
             ON a.attrelid = i.indrelid AND a.attnum = k.attnum
           WHERE k.ord <= i.indnkeyatts
           ORDER BY k.ord
       ) AS columns
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND i.indisunique AND NOT i.indisprimary
  AND i.indexprs IS NULL
ORDER BY n.nspname, c.relname
"""

_PG_CONSTRAINTS = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       con.conname, con.contype,
       ARRAY(
           SELECT a.attname
           FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute a
             ON a.attrelid = con.conrelid AND a.attnum = k.attnum
           ORDER BY k.ord
       ) AS child_columns,
       pn.nspname AS parent_schema, pc.relname AS parent_table,
       ARRAY(
           SELECT a.attname
           FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute a
             ON a.attrelid = con.confrelid AND a.attnum = k.attnum
           ORDER BY k.ord
       ) AS parent_columns
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class pc ON pc.oid = con.confrelid
LEFT JOIN pg_namespace pn ON pn.oid = pc.relnamespace
WHERE n.nspname = ANY(%s) AND con.contype IN ('p', 'u', 'f')
ORDER BY n.nspname, c.relname, con.conname
"""


def _reflect_postgres(target: DbTarget, schemas: list[str]) -> Reflection:
    conn = _connect(target)
    try:
        cursor = conn.cursor()
        try:
            if not schemas:
                cursor.execute("SELECT current_schema()")
                current = cursor.fetchone()
                schemas = [str(current[0])] if current and current[0] else []
            if not schemas:
                raise ReflectError("reflection_schema_unknown")
            cursor.execute(_PG_TABLES, (schemas,))
            table_rows = cursor.fetchall()
            cursor.execute(_PG_COLUMNS, (schemas,))
            column_rows = cursor.fetchall()
            cursor.execute(_PG_ENUMS)
            enum_rows = cursor.fetchall()
            cursor.execute(_PG_CONSTRAINTS, (schemas,))
            constraint_rows = cursor.fetchall()
            cursor.execute(_PG_UNIQUE_INDEXES, (schemas,))
            unique_rows = cursor.fetchall()
        finally:
            cursor.close()
    finally:
        conn.close()

    qualified = len(schemas) > 1
    enum_map: dict[Any, list[str]] = {}
    for type_oid, label in enum_rows:
        enum_map.setdefault(type_oid, []).append(str(label))

    ddl = ParsedDDL()
    tables: dict[tuple[str, str], ParsedTable] = {}
    for schema, name, comment in table_rows:
        name = str(name)
        table = ParsedTable(name=_qualified(str(schema), name, qualified))
        table.comment = str(comment or "")
        tables[(str(schema), name)] = table
        ddl.tables.append(table)
    if len(ddl.tables) > MAX_TABLES:
        raise ReflectError(f"表数量超过上限 {MAX_TABLES}")

    for schema, table_name, column_name, type_text, not_null, default, comment, type_oid in column_rows:
        table = tables.get((str(schema), str(table_name)))
        if table is None:
            continue
        column = ParsedColumn(
            name=str(column_name),
            type=normalize_type(str(type_text or "")),
            nullable=not bool(not_null),
            default=_scalar(default),
            comment=str(comment or "")[:1_000],
        )
        labels = enum_map.get(type_oid) or []
        if labels:
            column.enum_values = list(labels)
            column.type = _enum_type(labels)
        table.columns.append(column)

    index = {(str(schema), str(name)): table for (schema, name), table in tables.items()}
    for schema, table_name, _name, kind, child_columns, parent_schema, parent_table, parent_columns in constraint_rows:
        table = index.get((str(schema), str(table_name)))
        if table is None:
            continue
        columns = table.column_index()
        names = [str(item) for item in (child_columns or [])]
        if kind == "p":
            table.primary_key = names
            for name in names:
                column = columns.get(name)
                if column is not None:
                    column.primary_key = True
                    column.nullable = False
        elif kind == "u":
            if len(names) == 1 and names[0] in columns:
                columns[names[0]].unique = True
            elif len(names) > 1:
                ddl.warnings.append(
                    f"unique_composite_ignored:{table.name}:{','.join(names)}"
                )
        elif kind == "f":
            parent_name = _qualified(
                str(parent_schema or ""), str(parent_table or ""), qualified
            )
            if (str(parent_schema or ""), str(parent_table or "")) not in index:
                ddl.warnings.append(
                    f"fk_parent_outside_reflection:{table.name}->{parent_name}"
                )
                continue
            table.foreign_keys.append(
                ParsedForeignKey(
                    child_table=table.name,
                    child_columns=names,
                    parent_table=parent_name,
                    parent_columns=[str(item) for item in (parent_columns or [])],
                )
            )
    seen_schemas = {str(row[0]) for row in table_rows}
    for schema in schemas:
        if schema not in seen_schemas:
            ddl.warnings.append(f"schema_empty:{schema}")
    for schema, table_name, index_columns in unique_rows:
        table = index.get((str(schema), str(table_name)))
        if table is None:
            continue
        names = [str(item) for item in (index_columns or [])]
        columns = table.column_index()
        if len(names) == 1 and names[0] in columns:
            columns[names[0]].unique = True
        elif len(names) > 1:
            ddl.warnings.append(
                f"unique_composite_ignored:{table.name}:{','.join(names)}"
            )
    return Reflection(
        ddl=ddl,
        dialect="postgres",
        schemas=schemas,
        sources={
            "kind": "reflection",
            "dialect": "postgres",
            "schemas": schemas,
            "display": target.display,
        },
    )


# ────────────────────────────── MySQL ──────────────────────────────

_MYSQL_TABLES = """
SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_COMMENT
FROM information_schema.TABLES
WHERE TABLE_SCHEMA IN ({markers}) AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

_MYSQL_COLUMNS = """
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
       COLUMN_DEFAULT, COLUMN_KEY, COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA IN ({markers})
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
"""

_MYSQL_UNIQUE = """
SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA IN ({markers}) AND NON_UNIQUE = 0
ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
"""

_MYSQL_FKS = """
SELECT TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME,
       REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME,
       REFERENCED_COLUMN_NAME, ORDINAL_POSITION
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA IN ({markers}) AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
"""


def _reflect_mysql(target: DbTarget, schemas: list[str]) -> Reflection:
    conn = _connect(target)
    try:
        cursor = conn.cursor()
        try:
            if not schemas:
                cursor.execute("SELECT DATABASE()")
                current = cursor.fetchone()
                schemas = [str(current[0])] if current and current[0] else []
            if not schemas:
                raise ReflectError("reflection_schema_unknown")
            markers = ", ".join(["%s"] * len(schemas))
            params = list(schemas)
            cursor.execute(_MYSQL_TABLES.format(markers=markers), params)
            table_rows = cursor.fetchall()
            cursor.execute(_MYSQL_COLUMNS.format(markers=markers), params)
            column_rows = cursor.fetchall()
            cursor.execute(_MYSQL_UNIQUE.format(markers=markers), params)
            unique_rows = cursor.fetchall()
            cursor.execute(_MYSQL_FKS.format(markers=markers), params)
            fk_rows = cursor.fetchall()
        finally:
            cursor.close()
    finally:
        conn.close()

    qualified = len(schemas) > 1
    ddl = ParsedDDL()
    tables: dict[tuple[str, str], ParsedTable] = {}
    for schema, name, comment in table_rows:
        schema, name = _text(schema), _text(name)
        table = ParsedTable(name=_qualified(schema, name, qualified))
        table.comment = _text(comment)
        tables[(schema, name)] = table
        ddl.tables.append(table)
    if len(ddl.tables) > MAX_TABLES:
        raise ReflectError(f"表数量超过上限 {MAX_TABLES}")

    for schema, table_name, column_name, column_type, nullable, default, key, comment in column_rows:
        table = tables.get((_text(schema), _text(table_name)))
        if table is None:
            continue
        raw_type = _text(column_type)
        type_text = normalize_type(raw_type)
        column = ParsedColumn(
            name=_text(column_name),
            type=type_text,
            nullable=_text(nullable).upper() != "NO",
            default=_scalar(default),
            comment=_text(comment)[:1_000],
            primary_key=_text(key).upper() == "PRI",
        )
        if type_text.upper().startswith(("ENUM(", "SET(")):
            body = type_text[type_text.find("(") :]
            column.enum_values = parse_enum_values(body)
        if column.primary_key:
            column.nullable = False
        table.columns.append(column)

    for table in tables.values():
        table.primary_key = [
            column.name for column in table.columns if column.primary_key
        ]

    unique_groups: dict[tuple[str, str, str], list[str]] = {}
    for schema, table_name, index_name, column_name, _seq in unique_rows:
        if _text(index_name).upper() == "PRIMARY":
            continue
        key = (_text(schema), _text(table_name), _text(index_name))
        unique_groups.setdefault(key, []).append(_text(column_name))
    for (schema, table_name, _index_name), names in unique_groups.items():
        table = tables.get((schema, table_name))
        if table is None:
            continue
        columns = table.column_index()
        if len(names) == 1 and names[0] in columns:
            columns[names[0]].unique = True
        elif len(names) > 1:
            ddl.warnings.append(
                f"unique_composite_ignored:{table.name}:{','.join(names)}"
            )

    index = {key: table for key, table in tables.items()}
    grouped: dict[tuple[str, str, str], list[tuple[str, str, str, str]]] = {}
    for schema, table_name, constraint, column_name, parent_schema, parent_table, parent_column, _position in fk_rows:
        key = (_text(schema), _text(table_name), _text(constraint))
        grouped.setdefault(key, []).append(
            (
                _text(column_name),
                _text(parent_schema),
                _text(parent_table),
                _text(parent_column),
            )
        )
    for (schema, table_name, constraint), items in grouped.items():
        table = tables.get((schema, table_name))
        if table is None:
            continue
        child_columns: list[str] = []
        parent_columns: list[str] = []
        parent_schema = ""
        parent_table = ""
        valid = True
        for child, p_schema, p_table, p_column in items:
            if parent_schema and (p_schema, p_table) != (parent_schema, parent_table):
                valid = False
                break
            parent_schema, parent_table = p_schema, p_table
            child_columns.append(child)
            parent_columns.append(p_column)
        if not valid or not parent_table:
            ddl.warnings.append(f"fk_unmapped:{table.name}:{constraint}")
            continue
        parent_name = _qualified(parent_schema, parent_table, qualified)
        if (parent_schema, parent_table) not in index:
            ddl.warnings.append(
                f"fk_parent_outside_reflection:{table.name}->{parent_name}"
            )
            continue
        table.foreign_keys.append(
            ParsedForeignKey(
                child_table=table.name,
                child_columns=child_columns,
                parent_table=parent_name,
                parent_columns=parent_columns,
            )
        )

    seen_schemas = {_text(row[0]) for row in table_rows}
    for schema in schemas:
        if schema not in seen_schemas:
            ddl.warnings.append(f"schema_empty:{schema}")
    return Reflection(
        ddl=ddl,
        dialect="mysql",
        schemas=schemas,
        sources={
            "kind": "reflection",
            "dialect": "mysql",
            "schemas": schemas,
            "display": target.display,
        },
    )


# ──────────────────────────── 公共小工具 ────────────────────────────


def _connect(target: DbTarget):
    try:
        return connect(target)
    except DbConnectError as exc:
        raise ReflectError(str(exc)) from exc


def _qualified(schema: str, table: str, qualified: bool) -> str:
    if not qualified or not schema:
        return table
    return f"{schema}.{table}"


def _scalar(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _enum_type(values: list[str]) -> str:
    body = ", ".join("'" + str(item).replace("'", "''") + "'" for item in values)
    return f"ENUM({body})"


__all__ = ["ReflectError", "Reflection", "reflect_database"]
