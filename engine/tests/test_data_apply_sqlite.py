"""SQLite 造数执行：apply/teardown、失败回滚、非 SQLite 显式拒绝。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from astsight.data.apply import (
    ApplyError,
    apply_plan,
    parse_sqlite_url,
    teardown_plan,
)
from astsight.data.plan import build_data_plan
from contracts import (
    ColumnCatalog,
    DataCatalog,
    Evidence,
    TableCatalog,
    TestCase as CaseModel,
)


def _catalog() -> DataCatalog:
    return DataCatalog(
        schema_version=1,
        catalog_id="cat-items",
        project="demo",
        tables_order=["items"],
        tables=[
            TableCatalog(
                name="items",
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
                        name="name",
                        type="TEXT",
                        nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                ],
                evidence=Evidence.VERIFIED,
            )
        ],
    )


def _case() -> CaseModel:
    return CaseModel(
        id="TC-0001",
        endpoint="GET /items",
        category="positive",
        method="GET",
        path="/items",
        source="test",
    )


def _plan(rows: list[dict]):
    plan, rejected, _ = build_data_plan(
        case=_case(),
        catalog=_catalog(),
        fixtures={"items": rows},
        run_scope="r1",
    )
    assert rejected is None, rejected
    assert plan is not None
    return plan


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )


def _count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_apply_keep_then_teardown(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _make_db(db_path)
    plan = _plan([{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}])

    applied = apply_plan(db_url=_url(db_path), plan=plan, keep=True)
    assert applied.applied == 2
    assert applied.deleted == 0
    assert _count(db_path) == 2

    torn = teardown_plan(db_url=_url(db_path), plan=plan)
    assert torn.deleted == 2
    assert _count(db_path) == 0


def test_apply_can_teardown_in_same_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _make_db(db_path)
    plan = _plan([{"id": 1, "name": "alpha"}])

    result = apply_plan(db_url=_url(db_path), plan=plan, keep=False, teardown=True)
    assert result.applied == 1
    assert result.deleted == 1
    assert _count(db_path) == 0


def test_failed_insert_rolls_back_whole_plan(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _make_db(db_path)
    # 主键重复不会被 fixture 静态校验发现，由数据库在事务内拒绝。
    plan = _plan([{"id": 1, "name": "alpha"}, {"id": 1, "name": "beta"}])

    with pytest.raises(ApplyError, match="事务已回滚"):
        apply_plan(db_url=_url(db_path), plan=plan, keep=True)
    assert _count(db_path) == 0


def test_missing_table_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    plan = _plan([{"id": 1, "name": "alpha"}])

    with pytest.raises(ApplyError, match="数据库中不存在表"):
        apply_plan(db_url=_url(db_path), plan=plan, keep=True)


def test_tampered_sql_is_rejected_before_execution(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _make_db(db_path)
    plan = _plan([{"id": 1, "name": "alpha"}])
    plan.statements[0]["sql"] = "DROP TABLE items"

    with pytest.raises(ApplyError, match="plan 语句不合法"):
        apply_plan(db_url=_url(db_path), plan=plan, keep=True)
    # 表仍在。
    assert _count(db_path) == 0


def test_non_sqlite_dialect_is_unsupported() -> None:
    with pytest.raises(ApplyError, match="unsupported_dialect"):
        parse_sqlite_url("postgresql://user@localhost/app")
