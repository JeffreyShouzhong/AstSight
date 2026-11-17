"""CLI：data catalog 支持 DBML 与真实数据库反射。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from astsight.cli import main

DBML = """
Table customers {
  id integer [pk]
  email varchar(255) [not null, unique]
}

Table orders {
  id integer [pk]
  customer_id integer [not null, ref: > customers.id]
}
"""


def test_data_catalog_cli_accepts_dbml(tmp_path: Path, capsys) -> None:
    (tmp_path / "schema.dbml").write_text(DBML, encoding="utf-8")
    code = main(
        [
            "data",
            "catalog",
            "--project",
            str(tmp_path),
            "--dbml",
            "schema.dbml",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "dbml"
    assert payload["tables"] == 2
    catalog = json.loads(Path(payload["catalog"]).read_text(encoding="utf-8"))
    assert [table["name"] for table in catalog["tables"]] == [
        "customers",
        "orders",
    ]
    assert catalog["sources"]["kind"] == "dbml"
    assert catalog["foreign_keys"][0]["child_table"] == "orders"


def test_data_catalog_cli_reflects_sqlite(tmp_path: Path, capsys) -> None:
    database = tmp_path / "demo.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    code = main(
        [
            "data",
            "catalog",
            "--project",
            str(tmp_path),
            "--db-url",
            f"sqlite:///{database.as_posix()}",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "reflection"
    assert payload["dialect"] == "sqlite"
    assert payload["tables_order"] == ["customers", "orders"]

    # 方言由 URL 推断；显式冲突的 --dialect 必须失败。
    code = main(
        [
            "data",
            "catalog",
            "--project",
            str(tmp_path),
            "--db-url",
            f"sqlite:///{database.as_posix()}",
            "--dialect",
            "postgres",
            "--json",
        ]
    )
    assert code == 1
