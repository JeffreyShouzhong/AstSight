"""SQLite 造数/回收执行（stdlib sqlite3）。

v1 只支持 SQLite；其他方言显式 `unsupported_dialect`，不假装支持。
执行前再次校验 SQL 形状，防止被篡改的 plan 注入任意语句。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from contracts import DataPlan, DataPlanBundle

_IDENT = r'"[A-Za-z_][A-Za-z0-9_$]*"'
_INSERT_RE = re.compile(
    rf'^INSERT INTO {_IDENT} \({_IDENT}(?:, {_IDENT})*\) '
    rf'VALUES \(\?(?:, \?)*\)$'
)
_DELETE_RE = re.compile(
    rf'^DELETE FROM {_IDENT} WHERE {_IDENT} = \?(?: AND {_IDENT} = \?)*$'
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
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "db_path": self.db_path,
            "applied": self.applied,
            "deleted": self.deleted,
            "kept": self.kept,
            "warnings": list(self.warnings),
        }


def parse_sqlite_url(db_url: str) -> Path:
    value = (db_url or "").strip()
    if not value.startswith("sqlite:///"):
        raise ApplyError(
            f"unsupported_dialect: 仅支持 sqlite:///，实际 {value.split(':', 1)[0]!r}"
        )
    raw = value[len("sqlite:///") :]
    if not raw:
        raise ApplyError("sqlite 路径为空")
    path = Path(raw)
    if str(path) in {":memory:", ""}:
        raise ApplyError("不支持内存数据库：造数需要可回收的持久文件")
    return path.resolve()


def _check_statement(statement: dict[str, Any], kind: str) -> None:
    sql = str(statement.get("sql") or "")
    pattern = _INSERT_RE if kind == "insert" else _DELETE_RE
    if statement.get("kind") != kind or not pattern.match(sql):
        raise ApplyError(f"plan 语句不合法: {sql[:160]!r}")
    params = statement.get("params")
    if not isinstance(params, list):
        raise ApplyError("plan 语句 params 必须是 list")


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ApplyError(f"sqlite 数据库不存在: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _check_tables(conn: sqlite3.Connection, statements: Iterable[dict[str, Any]]) -> None:
    names = {
        str(item.get("table") or "")
        for item in statements
        if item.get("table")
    }
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    existing = {str(row[0]) for row in rows}
    missing = sorted(names - existing)
    if missing:
        raise ApplyError(f"数据库中不存在表: {missing}")


def apply_plan(
    *,
    db_url: str,
    plan: DataPlan,
    keep: bool = True,
    teardown: bool = False,
) -> ApplyResult:
    db_path = parse_sqlite_url(db_url)
    inserts = list(plan.statements or [])
    for statement in inserts:
        _check_statement(statement, "insert")
    deletes = list(plan.teardown) if isinstance(plan.teardown, list) else []
    for statement in deletes:
        _check_statement(statement, "delete")

    result = ApplyResult(
        dialect="sqlite",
        db_path=str(db_path),
        kept=keep,
        warnings=list(plan.warnings),
    )
    conn = _connect(db_path)
    try:
        _check_tables(conn, [*inserts, *deletes])
        with conn:
            for statement in inserts:
                conn.execute(statement["sql"], statement["params"])
                result.applied += 1
            if teardown and not keep:
                for statement in deletes:
                    conn.execute(statement["sql"], statement["params"])
                    result.deleted += 1
    except sqlite3.Error as exc:
        raise ApplyError(f"sqlite 执行失败，事务已回滚: {exc}") from exc
    finally:
        conn.close()
    return result


def teardown_plan(*, db_url: str, plan: DataPlan) -> ApplyResult:
    db_path = parse_sqlite_url(db_url)
    deletes = list(plan.teardown) if isinstance(plan.teardown, list) else []
    for statement in deletes:
        _check_statement(statement, "delete")
    result = ApplyResult(dialect="sqlite", db_path=str(db_path))
    conn = _connect(db_path)
    try:
        _check_tables(conn, deletes)
        with conn:
            for statement in deletes:
                conn.execute(statement["sql"], statement["params"])
                result.deleted += 1
    except sqlite3.Error as exc:
        raise ApplyError(f"sqlite teardown 失败: {exc}") from exc
    finally:
        conn.close()
    return result


def apply_bundle(
    *,
    db_url: str,
    bundle: DataPlanBundle,
    run_scope: str | None = None,
    keep: bool = False,
) -> ApplyResult:
    db_path = parse_sqlite_url(db_url)
    plans = [
        plan
        for plan in bundle.plans
        if not run_scope or plan.run_scope == run_scope
    ]
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
    result = ApplyResult(
        dialect="sqlite",
        db_path=str(db_path),
        kept=keep,
        warnings=list(bundle.warnings),
    )
    conn = _connect(db_path)
    try:
        _check_tables(conn, [*inserts, *deletes])
        with conn:
            for statement in inserts:
                conn.execute(statement["sql"], statement["params"])
                result.applied += 1
            if not keep:
                for statement in reversed(deletes):
                    conn.execute(statement["sql"], statement["params"])
                    result.deleted += 1
    except sqlite3.Error as exc:
        raise ApplyError(f"sqlite 执行失败，事务已回滚: {exc}") from exc
    finally:
        conn.close()
    return result


__all__ = [
    "ApplyError",
    "ApplyResult",
    "apply_bundle",
    "apply_plan",
    "parse_sqlite_url",
    "teardown_plan",
]
