"""SQLite 反射：主键、隐式外键、唯一索引与表过滤。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from astsight.data import (
    ReflectError,
    build_catalog,
    parse_db_url,
    reflect_database,
    validate_catalog,
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "demo.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE UNIQUE INDEX ux_orders_status ON orders(status);
            CREATE TABLE tags (
                id INTEGER PRIMARY KEY,
                order_id INTEGER,
                label TEXT
            );
            CREATE UNIQUE INDEX ux_tags_composite ON tags(order_id, label);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_sqlite_reflection_builds_catalog(tmp_path: Path) -> None:
    path = _database(tmp_path)
    reflection = reflect_database(f"sqlite:///{path.as_posix()}")
    assert reflection.dialect == "sqlite"
    tables = reflection.ddl.table_index()
    assert list(tables) == ["customers", "orders", "tags"]

    customers = tables["customers"]
    assert customers.primary_key == ["id"]
    assert customers.column_index()["email"].unique is True

    orders = tables["orders"]
    assert orders.primary_key == ["id"]
    status = orders.column_index()["status"]
    assert status.unique is True
    assert status.default == "pending"
    # 省略列名的 REFERENCES 通过 PRAGMA 解析到父表主键。
    fks = orders.foreign_keys
    assert len(fks) == 1
    assert fks[0].child_columns == ["customer_id"]
    assert fks[0].parent_table == "customers"
    assert fks[0].parent_columns == ["id"]
    assert (
        "unique_composite_ignored:tags:order_id,label"
        in reflection.ddl.warnings
    )

    catalog = build_catalog(
        project="demo", ddl=reflection.ddl, dialect="sqlite"
    )
    assert validate_catalog(catalog) == []
    assert catalog.tables_order == ["customers", "orders", "tags"]


def test_sqlite_reflection_filters_tables_and_drops_dangling_fks(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    reflection = reflect_database(
        f"sqlite:///{path.as_posix()}", include=["orders*"]
    )
    assert [table.name for table in reflection.ddl.tables] == ["orders"]
    assert reflection.ddl.tables[0].foreign_keys == []
    assert "reflection_filtered:2" in reflection.ddl.warnings


def test_reflect_rejects_unknown_sqlite_file(tmp_path: Path) -> None:
    target = parse_db_url(f"sqlite:///{(tmp_path / 'nope.db').as_posix()}")
    assert target.sqlite_path is not None
    with pytest.raises(ReflectError, match="sqlite 数据库不存在"):
        reflect_database(target.url)
