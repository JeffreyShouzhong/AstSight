"""真实数据库联调（默认跳过）。

设置以下环境变量后运行；测试会创建并删除临时表，不要指向生产库：

```
ASTSIGHT_TEST_POSTGRES_URL=postgresql://user:pass@host:5432/testdb
ASTSIGHT_TEST_MYSQL_URL=mysql://user:pass@host:3306/testdb
```

未设置的环境变量对应参数会显式 SKIP，不伪装成通过。
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import unquote, urlsplit

import pytest

from astsight.data import (
    apply_plan,
    build_catalog,
    build_data_plan,
    parse_ddl,
    teardown_plan,
)
from contracts import TestCase as CaseModel

_LIVE = [
    ("ASTSIGHT_TEST_POSTGRES_URL", "postgres", "INTEGER"),
    ("ASTSIGHT_TEST_MYSQL_URL", "mysql", "INT"),
]


def _case() -> CaseModel:
    return CaseModel(
        id="TC-LIVE",
        endpoint="GET /items",
        category="positive",
        method="GET",
        path="/items",
        source="live",
    )


def _connect(db_url: str, dialect: str):
    if dialect == "postgres":
        import psycopg

        # 测试辅助连接只执行 DDL，自动提交避免 close 时回滚建表。
        return psycopg.connect(db_url, autocommit=True)
    import pymysql

    split = urlsplit(db_url)
    return pymysql.connect(
        host=split.hostname or "127.0.0.1",
        port=split.port or 3306,
        user=unquote(split.username or ""),
        password=unquote(split.password or ""),
        database=unquote(split.path.lstrip("/")),
        charset="utf8mb4",
        autocommit=True,
    )


def _execute(db_url: str, dialect: str, sql: str) -> None:
    conn = _connect(db_url, dialect)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
    finally:
        conn.close()


@pytest.mark.parametrize(("env_name", "dialect", "id_type"), _LIVE)
def test_live_apply_and_teardown(env_name: str, dialect: str, id_type: str) -> None:
    db_url = os.environ.get(env_name)
    if not db_url:
        pytest.skip(f"{env_name} 未配置")
    table = f"astsight_live_{uuid.uuid4().hex[:12]}"
    ddl = (
        f"CREATE TABLE {table} "
        f"(id {id_type} PRIMARY KEY, name VARCHAR(32) NOT NULL);"
    )
    _execute(db_url, dialect, ddl)
    try:
        catalog = build_catalog(
            project="live", ddl=parse_ddl(ddl, dialect=dialect), dialect=dialect
        )
        plan, rejected, _ = build_data_plan(
            case=_case(),
            catalog=catalog,
            fixtures={table: [{"id": 1, "name": "alpha"}]},
            run_scope="live",
        )
        assert rejected is None, rejected
        assert plan is not None

        applied = apply_plan(db_url=db_url, plan=plan, keep=True)
        assert applied.dialect == dialect
        assert applied.applied == 1

        torn = teardown_plan(db_url=db_url, plan=plan)
        assert torn.deleted == 1
    finally:
        _execute(db_url, dialect, f"DROP TABLE IF EXISTS {table}")
