"""多行 fixtures：跨行引用、父子外键补全、SQLite 应用与回收。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from astsight.data.apply import apply_plan, teardown_plan
from astsight.data.fixtures import prepare_plan_fixtures
from astsight.data.plan import build_data_plan
from contracts import (
    ColumnCatalog,
    DataCatalog,
    Evidence,
    ForeignKey,
    TableCatalog,
    TestCase as CaseModel,
)


def _catalog() -> DataCatalog:
    return DataCatalog(
        schema_version=1,
        catalog_id="cat-parent-child",
        project="demo",
        tables_order=["orders", "order_items"],
        tables=[
            TableCatalog(
                name="orders",
                primary_key=["id"],
                columns=[
                    ColumnCatalog(
                        name="id",
                        type="INTEGER",
                        nullable=False,
                        primary_key=True,
                        evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="status",
                        type="TEXT",
                        nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                ],
                evidence=Evidence.VERIFIED,
            ),
            TableCatalog(
                name="order_items",
                primary_key=["id"],
                columns=[
                    ColumnCatalog(
                        name="id",
                        type="INTEGER",
                        nullable=False,
                        primary_key=True,
                        evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="order_id",
                        type="INTEGER",
                        nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="sku",
                        type="TEXT",
                        nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                ],
                evidence=Evidence.VERIFIED,
            ),
        ],
        foreign_keys=[
            ForeignKey(
                child_table="order_items",
                child_columns=["order_id"],
                parent_table="orders",
                parent_columns=["id"],
                evidence=Evidence.VERIFIED,
            )
        ],
    )


def _case() -> CaseModel:
    return CaseModel(
        id="TC-MULTI",
        endpoint="GET /orders/summary",
        category="positive",
        method="GET",
        path="/orders/summary",
        source="test",
    )


def _fixtures() -> dict[str, list[dict]]:
    return {
        "orders": [
            {"id": 10, "status": "pending"},
            {"id": 20, "status": "paid"},
        ],
        "order_items": [
            {"id": 100, "order_id": {"$ref": "orders[0].id"}, "sku": "A"},
            {"id": 101, "order_id": {"$ref": "orders[0].id"}, "sku": "B"},
            {"id": 102, "order_id": {"$ref": "orders[1].id"}, "sku": "C"},
        ],
    }


def test_cross_row_refs_resolve_for_multiple_parents_and_children() -> None:
    fixtures, errors, warnings = prepare_plan_fixtures(_catalog(), _fixtures())

    assert errors == []
    assert warnings == []
    assert [
        row["order_id"] for row in fixtures["order_items"]
    ] == [10, 10, 20]


def test_single_parent_autofills_missing_child_foreign_key() -> None:
    fixtures, errors, warnings = prepare_plan_fixtures(
        _catalog(),
        {
            "orders": [{"id": 10, "status": "pending"}],
            "order_items": [
                {"id": 100, "sku": "A"},
                {"id": 101, "sku": "B"},
            ],
        },
    )

    assert errors == []
    assert [row["order_id"] for row in fixtures["order_items"]] == [10, 10]
    assert (
        "fixture_fk_autofilled:order_items->orders:rows=2" in warnings
    )


def test_multiple_parents_require_explicit_child_links() -> None:
    fixtures, errors, _warnings = prepare_plan_fixtures(
        _catalog(),
        {
            "orders": [
                {"id": 10, "status": "pending"},
                {"id": 20, "status": "paid"},
            ],
            "order_items": [{"id": 100, "sku": "A"}],
        },
    )

    assert fixtures["order_items"][0].get("order_id") is None
    assert (
        "fixture_fk_ambiguous:order_items->orders:parents=2" in errors
    )

    plan, rejected, _warnings = build_data_plan(
        case=_case(),
        catalog=_catalog(),
        fixtures={
            "orders": [
                {"id": 10, "status": "pending"},
                {"id": 20, "status": "paid"},
            ],
            "order_items": [{"id": 100, "sku": "A"}],
        },
    )
    assert plan is None
    assert rejected is not None
    assert rejected.reason == "fixture_relation_invalid"
    assert "fixture_fk_ambiguous" in rejected.details


def test_multrow_parent_child_plan_and_sqlite_teardown(tmp_path: Path) -> None:
    plan, rejected, warnings = build_data_plan(
        case=_case(),
        catalog=_catalog(),
        fixtures=_fixtures(),
        run_scope="multi-row",
    )

    assert rejected is None, rejected
    assert warnings == []
    assert plan is not None
    assert [row["order_id"] for row in plan.fixtures["order_items"]] == [
        10,
        10,
        20,
    ]
    assert [item["table"] for item in plan.statements] == [
        "orders",
        "orders",
        "order_items",
        "order_items",
        "order_items",
    ]
    assert [item["table"] for item in plan.teardown] == [
        "order_items",
        "order_items",
        "order_items",
        "orders",
        "orders",
    ]

    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE orders (
              id INTEGER PRIMARY KEY,
              status TEXT NOT NULL
            );
            CREATE TABLE order_items (
              id INTEGER PRIMARY KEY,
              order_id INTEGER NOT NULL REFERENCES orders(id),
              sku TEXT NOT NULL
            );
            """
        )
    db_url = f"sqlite:///{db_path.as_posix()}"

    applied = apply_plan(db_url=db_url, plan=plan, keep=True)
    assert applied.applied == 5
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM order_items"
        ).fetchone()[0] == 3

    torn = teardown_plan(db_url=db_url, plan=plan)
    assert torn.deleted == 5
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM order_items"
        ).fetchone()[0] == 0
