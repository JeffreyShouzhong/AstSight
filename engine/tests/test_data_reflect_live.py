"""真实数据库反射联调（默认跳过）。

```
ASTSIGHT_TEST_POSTGRES_URL=postgresql://user:pass@host:5432/testdb
ASTSIGHT_TEST_MYSQL_URL=mysql://user:pass@host:3306/testdb
```

测试会创建并删除两张临时表，不要指向生产库。
"""
from __future__ import annotations

import os
import uuid

import pytest

from astsight.data import (
    build_catalog,
    reflect_database,
    validate_catalog,
)
from astsight.data.connection import DbConnectError, connect
from astsight.data.dialects import parse_db_url

_LIVE = [
    ("ASTSIGHT_TEST_POSTGRES_URL", "postgres"),
    ("ASTSIGHT_TEST_MYSQL_URL", "mysql"),
]


def _ddl(dialect: str, parent: str, child: str) -> list[str]:
    id_column = (
        "SERIAL PRIMARY KEY" if dialect == "postgres" else "INT AUTO_INCREMENT PRIMARY KEY"
    )
    return [
        f"CREATE TABLE {parent} ("
        f"id {id_column}, name VARCHAR(32) NOT NULL UNIQUE)",
        f"CREATE TABLE {child} ("
        f"id {id_column}, {parent}_id INTEGER NOT NULL, "
        f"CONSTRAINT fk_{child}_parent FOREIGN KEY ({parent}_id) "
        f"REFERENCES {parent}(id))",
    ]


@pytest.mark.parametrize("env_name,dialect", _LIVE)
def test_reflection_reads_pk_unique_and_fk(
    env_name: str, dialect: str
) -> None:
    url = os.environ.get(env_name)
    if not url:
        pytest.skip(f"{env_name} 未设置")
    suffix = uuid.uuid4().hex[:8]
    parent = f"reflect_parent_{suffix}"
    child = f"reflect_child_{suffix}"
    try:
        conn = connect(parse_db_url(url))
    except DbConnectError as exc:
        pytest.skip(f"驱动/连接不可用: {exc}")

    try:
        cursor = conn.cursor()
        try:
            for statement in _ddl(dialect, parent, child):
                cursor.execute(statement)
        finally:
            cursor.close()
        conn.commit()

        reflection = reflect_database(url, include=[f"{parent}*", f"{child}*"])
        tables = reflection.ddl.table_index()
        assert set(tables) == {parent, child}
        assert tables[parent].primary_key == ["id"]
        assert tables[parent].column_index()["name"].unique is True
        fks = tables[child].foreign_keys
        assert len(fks) == 1
        assert fks[0].parent_table == parent
        assert fks[0].parent_columns == ["id"]

        catalog = build_catalog(
            project="live", ddl=reflection.ddl, dialect=dialect
        )
        assert validate_catalog(catalog) == []
        assert catalog.tables_order == [parent, child]
    finally:
        cursor = conn.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {child}")
            cursor.execute(f"DROP TABLE IF EXISTS {parent}")
        finally:
            cursor.close()
        conn.commit()
        conn.close()
