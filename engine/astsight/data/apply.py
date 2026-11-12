"""造数/回收执行：SQLite（stdlib）、PostgreSQL（psycopg）、MySQL（PyMySQL）。

执行前再次校验 SQL 形状，防止被篡改的 plan 注入任意语句。
方言由 `--db-url` 决定；连接驱动按需导入，未安装时显式报 `db_driver_missing`。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from contracts import DataPlan, DataPlanBundle

from .dialects import DbTarget, DialectError, parse_db_url

_IDENT = r'["`][A-Za-z_][A-Za-z0-9_$]*["`]'
_VALUE = r"(?:\?|%s)"
_INSERT_RE = re.compile(
    rf"^INSERT INTO {_IDENT} \({_IDENT}(?:, {_IDENT})*\) "
    rf"VALUES \({_VALUE}(?:, {_VALUE})*\)$"
)
_DELETE_RE = re.compile(
    rf"^DELETE FROM {_IDENT} WHERE {_IDENT} = {_VALUE}"
    rf"(?: AND {_IDENT} = {_VALUE})*$"
)


class ApplyError(RuntimeError):
    """数据库不可用或 plan 语句不合法。"""


@dataclass
class ApplyResult:
    dialect: str
    db_path: str
    applied: int = 0
    deleted: int = 0
    kept: bool = False
    target: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "db_path": self.db_path,
            "target": self.target or self.db_path,
            "applied": self.applied,
            "deleted": self.deleted,
            "kept": self.kept,
            "warnings": list(self.warnings),
        }


def parse_sqlite_url(db_url: str) -> Path:
    """兼容旧接口：只接受 sqlite:/// 文件路径。"""
    value = (db_url or "").strip()
    if not value.startswith("sqlite:///"):
        raise ApplyError(
            f"unsupported_dialect: 仅支持 sqlite:///，实际 {value.split(':', 1)[0]!r}"
        )
    try:
        target = parse_db_url(value)
    except DialectError as exc:
        raise ApplyError(str(exc)) from exc
    if target.sqlite_path is None:
        raise ApplyError("sqlite 路径为空")
    return target.sqlite_path


def _target(db_url: str) -> DbTarget:
    try:
        return parse_db_url(db_url)
    except DialectError as exc:
        raise ApplyError(str(exc)) from exc


def _check_statement(statement: dict[str, Any], kind: str) -> None:
    sql = str(statement.get("sql") or "")
    pattern = _INSERT_RE if kind == "insert" else _DELETE_RE
    if statement.get("kind") != kind or not pattern.match(sql):
        raise ApplyError(f"plan 语句不合法: {sql[:160]!r}")
    params = statement.get("params")
    if not isinstance(params, list):
        raise ApplyError("plan 语句 params 必须是 list")


def _connect(target: DbTarget):
    if target.dialect == "sqlite":
        path = target.sqlite_path
        assert path is not None
        if not path.is_file():
            raise ApplyError(f"sqlite 数据库不存在: {path}")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    if target.dialect == "postgres":
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ApplyError(
                "db_driver_missing: PostgreSQL 造数需要 psycopg，"
                "请安装 psycopg[binary]"
            ) from exc
        try:
            return psycopg.connect(target.url)
        except Exception as exc:  # 驱动异常类型随版本变化，统一转换
            raise ApplyError(f"postgres 连接失败: {exc}") from exc
    if target.dialect == "mysql":
        try:
            import pymysql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ApplyError(
                "db_driver_missing: MySQL 造数需要 PyMySQL，请安装 PyMySQL"
            ) from exc
        parts = urlsplit(target.url)
        kwargs: dict[str, Any] = {
            "host": parts.hostname or "127.0.0.1",
            "port": parts.port or 3306,
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "database": unquote(parts.path.lstrip("/")),
            "charset": target.options.get("charset", "utf8mb4"),
            "autocommit": False,
        }
        timeout = target.options.get("connect_timeout")
        if timeout:
            try:
                kwargs["connect_timeout"] = int(timeout)
            except ValueError as exc:
                raise ApplyError("db_url_invalid: connect_timeout 必须是整数") from exc
        try:
            return pymysql.connect(**kwargs)
        except Exception as exc:
            raise ApplyError(f"mysql 连接失败: {exc}") from exc
    raise ApplyError(f"unsupported_dialect: {target.dialect!r}")


def _check_tables(conn, target: DbTarget, statements: Iterable[dict[str, Any]]) -> None:
    names = {
        str(item.get("table") or "")
        for item in statements
        if item.get("table")
    }
    if not names:
        return
    if target.dialect == "sqlite":
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    else:
        cursor = conn.cursor()
        try:
            if target.dialect == "postgres":
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                )
            else:
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
            rows = cursor.fetchall()
        finally:
            cursor.close()
    existing = {str(row[0]) for row in rows}
    missing = sorted(names - existing)
    if missing:
        raise ApplyError(f"数据库中不存在表: {missing}")


def _execute(conn, statements: Iterable[dict[str, Any]], counter: list[int]) -> None:
    cursor = conn.cursor()
    try:
        for statement in statements:
            cursor.execute(statement["sql"], statement["params"])
            counter[0] += 1
    finally:
        cursor.close()


def _run(
    *,
    target: DbTarget,
    inserts: list[dict[str, Any]],
    deletes: list[dict[str, Any]],
    keep: bool,
    teardown: bool,
    warnings: list[str],
) -> ApplyResult:
    result = ApplyResult(
        dialect=target.dialect,
        db_path=str(target.sqlite_path) if target.sqlite_path else target.display,
        target=target.display,
        kept=keep,
        warnings=list(warnings),
    )
    conn = _connect(target)
    applied = [0]
    deleted = [0]
    try:
        _check_tables(conn, target, [*inserts, *deletes])
        _execute(conn, inserts, applied)
        if teardown and not keep:
            _execute(conn, list(reversed(deletes)), deleted)
        conn.commit()
    except ApplyError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise ApplyError(
            f"{target.dialect} 执行失败，事务已回滚: {exc}"
        ) from exc
    finally:
        conn.close()
    result.applied = applied[0]
    result.deleted = deleted[0]
    return result


def apply_plan(
    *,
    db_url: str,
    plan: DataPlan,
    keep: bool = True,
    teardown: bool = False,
) -> ApplyResult:
    target = _target(db_url)
    inserts = list(plan.statements or [])
    for statement in inserts:
        _check_statement(statement, "insert")
    deletes = list(plan.teardown) if isinstance(plan.teardown, list) else []
    for statement in deletes:
        _check_statement(statement, "delete")
    if plan.dialect and plan.dialect != target.dialect:
        raise ApplyError(
            f"dialect_mismatch: plan 为 {plan.dialect}，数据库为 {target.dialect}"
        )
    return _run(
        target=target,
        inserts=inserts,
        deletes=deletes,
        keep=keep,
        teardown=teardown,
        warnings=list(plan.warnings),
    )


def teardown_plan(*, db_url: str, plan: DataPlan) -> ApplyResult:
    target = _target(db_url)
    deletes = list(plan.teardown) if isinstance(plan.teardown, list) else []
    for statement in deletes:
        _check_statement(statement, "delete")
    if plan.dialect and plan.dialect != target.dialect:
        raise ApplyError(
            f"dialect_mismatch: plan 为 {plan.dialect}，数据库为 {target.dialect}"
        )
    return _run(
        target=target,
        inserts=[],
        deletes=deletes,
        keep=False,
        teardown=True,
        warnings=[],
    )


def apply_bundle(
    *,
    db_url: str,
    bundle: DataPlanBundle,
    run_scope: str | None = None,
    keep: bool = False,
) -> ApplyResult:
    target = _target(db_url)
    plans = [
        plan
        for plan in bundle.plans
        if not run_scope or plan.run_scope == run_scope
    ]
    mismatched = sorted(
        {plan.dialect for plan in plans if plan.dialect and plan.dialect != target.dialect}
    )
    if mismatched:
        raise ApplyError(
            f"dialect_mismatch: plan 为 {mismatched}，数据库为 {target.dialect}"
        )
    inserts: list[dict[str, Any]] = []
    deletes: list[dict[str, Any]] = []
    for plan in plans:
        inserts.extend(plan.statements or [])
        if isinstance(plan.teardown, list):
            deletes.extend(plan.teardown)
    for statement in inserts:
        _check_statement(statement, "insert")
    for statement in deletes:
        _check_statement(statement, "delete")
    return _run(
        target=target,
        inserts=inserts,
        deletes=deletes,
        keep=keep,
        teardown=True,
        warnings=list(bundle.warnings),
    )


__all__ = [
    "ApplyError",
    "ApplyResult",
    "apply_bundle",
    "apply_plan",
    "parse_sqlite_url",
    "teardown_plan",
]
