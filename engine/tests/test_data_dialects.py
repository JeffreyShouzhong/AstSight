"""多方言造数：URL 解析、SQL 渲染与方言不匹配的显式拒绝。"""
from __future__ import annotations

import pytest

from astsight.data import (
    ApplyError,
    DialectError,
    apply_plan,
    build_catalog,
    build_data_plan,
    normalize_dialect,
    parse_db_url,
    parse_ddl,
)
from contracts import TestCase as CaseModel


def _case() -> CaseModel:
    return CaseModel(
        id="TC-0001",
        endpoint="GET /items",
        category="positive",
        method="GET",
        path="/items",
        source="test",
    )


def _catalog(dialect: str):
    ddl = parse_ddl(
        "CREATE TABLE items ("
        "id INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "active BOOLEAN NOT NULL DEFAULT 1"
        ")",
        dialect=dialect,
    )
    return build_catalog(project="demo", ddl=ddl, dialect=dialect)


def _plan(dialect: str):
    plan, rejected, _ = build_data_plan(
        case=_case(),
        catalog=_catalog(dialect),
        fixtures={"items": [{"id": 1, "name": "alpha", "active": True}]},
        run_scope="r1",
    )
    assert rejected is None, rejected
    assert plan is not None
    return plan


def test_normalize_dialect_aliases() -> None:
    assert normalize_dialect("postgresql") == "postgres"
    assert normalize_dialect("mariadb") == "mysql"
    assert normalize_dialect("SQLite") == "sqlite"
    with pytest.raises(DialectError, match="unsupported_dialect"):
        normalize_dialect("oracle")


@pytest.mark.parametrize(
    ("url", "dialect", "display"),
    [
        ("sqlite:///tmp/app.db", "sqlite", None),
        (
            "postgresql://app:secret@db.example.test:5432/appdb?sslmode=require",
            "postgres",
            "postgres://app:***@db.example.test:5432/appdb?sslmode=require",
        ),
        (
            "postgresql+psycopg://app:secret@localhost/appdb",
            "postgres",
            "postgres://app:***@localhost/appdb",
        ),
        (
            "mysql://app:secret@127.0.0.1:3306/appdb?charset=utf8mb4",
            "mysql",
            "mysql://app:***@127.0.0.1:3306/appdb?charset=utf8mb4",
        ),
    ],
)
def test_parse_db_url(url: str, dialect: str, display: str | None) -> None:
    target = parse_db_url(url)
    assert target.dialect == dialect
    if display is None:
        assert target.sqlite_path is not None
        assert target.display == str(target.sqlite_path)
    else:
        assert target.display == display
        assert "secret" not in target.display
        assert target.sqlite_path is None


def test_parse_db_url_rejects_missing_database_and_memory() -> None:
    with pytest.raises(DialectError, match="缺少数据库名"):
        parse_db_url("postgresql://user@localhost")
    with pytest.raises(DialectError, match="缺少数据库名"):
        parse_db_url("mysql://user@localhost")
    with pytest.raises(DialectError, match="内存数据库"):
        parse_db_url("sqlite:///:memory:")


@pytest.mark.parametrize(
    ("dialect", "quoted_table", "marker"),
    [
        ("sqlite", '"items"', "?"),
        ("postgres", '"items"', "%s"),
        ("mysql", "`items`", "%s"),
    ],
)
def test_render_sql_uses_dialect_quoting_and_placeholder(
    dialect: str, quoted_table: str, marker: str
) -> None:
    plan = _plan(dialect)
    assert plan.dialect == dialect
    insert = plan.statements[0]
    assert insert["sql"].startswith(f"INSERT INTO {quoted_table} ")
    assert insert["sql"].endswith(f"VALUES ({marker}, {marker}, {marker})")
    assert insert["params"][2] is (1 if dialect == "sqlite" else True)
    assert isinstance(plan.teardown, list)
    quote = "`" if dialect == "mysql" else '"'
    assert plan.teardown[0]["sql"] == (
        f"DELETE FROM {quote}items{quote} WHERE {quote}id{quote} = {marker}"
    )


def test_apply_rejects_plan_dialect_mismatch_before_connecting() -> None:
    plan = _plan("sqlite")
    with pytest.raises(ApplyError, match="dialect_mismatch"):
        apply_plan(
            db_url="postgresql://user:pass@127.0.0.1:1/appdb",
            plan=plan,
            keep=True,
        )


def test_mysql_and_postgres_ddl_parse() -> None:
    mysql = parse_ddl(
        "CREATE TABLE `partner` ("
        "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, "
        "`name` VARCHAR(64) NOT NULL COMMENT '姓名', "
        "PRIMARY KEY (`id`)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
        dialect="mysql",
    )
    assert [table.name for table in mysql.tables] == ["partner"]
    assert [column.name for column in mysql.tables[0].columns] == ["id", "name"]
    assert mysql.tables[0].primary_key == ["id"]

    postgres = parse_ddl(
        "CREATE TABLE partner ("
        "id SERIAL PRIMARY KEY, "
        "name VARCHAR(64) NOT NULL, "
        "tags JSONB DEFAULT '{}'::jsonb"
        ");",
        dialect="postgres",
    )
    assert [table.name for table in postgres.tables] == ["partner"]
    assert postgres.tables[0].primary_key == ["id"]
    assert [column.name for column in postgres.tables[0].columns] == [
        "id",
        "name",
        "tags",
    ]
