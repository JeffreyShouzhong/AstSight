"""SQLite 持久化层（P-03 示例）。

把写库动作收敛成 `insert_partner` / `insert_audit_log` 两个函数，方便验证
「变更发生在 DB 写入代码 → 反查调用链 → 命中 API」这条 delta 路径：
`register()` 调用它们，代码图谱因此能把 `db.insert_partner` 的改动回溯到
`POST /api/partner/register`。

数据库文件默认放在示例目录下，可用 `PARTNER_DB` 覆盖；`/_reset` 会清空表，
所以生成用例可以逐条重置状态。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
DEFAULT_DB_PATH = BASE_DIR / "partner.db"

_db_path = Path(os.getenv("PARTNER_DB", str(DEFAULT_DB_PATH))).expanduser()


def configure(path: str | Path) -> None:
    """切换数据库文件（测试或多实例隔离用）。"""
    global _db_path
    _db_path = Path(path).expanduser().resolve()


def db_path() -> Path:
    return _db_path


def connect() -> sqlite3.Connection:
    """每次调用返回独立连接；事务边界由调用方控制。"""
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_db_path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """一条连接一个事务：正常退出提交，异常回滚，最终关闭。"""
    connection = connect()
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


# 已有 partner.db 不会因为 CREATE TABLE IF NOT EXISTS 自动加列，这里做幂等补列。
_PARTNER_METADATA_COLUMNS = (
    "agreement_checked_at",
    "agreement_ip",
    "agreement_user_agent",
)


def init_db() -> None:
    with transaction() as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(partner)")
        }
        for column in _PARTNER_METADATA_COLUMNS:
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE partner ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )


def reset_db() -> None:
    """清空注册数据，并保留序号表的初始行。"""
    init_db()
    with transaction() as connection:
        connection.execute("DELETE FROM partner_audit_log")
        connection.execute("DELETE FROM partner")
        connection.execute("UPDATE partner_seq SET value = 0 WHERE name = 'partner'")
        connection.execute(
            "INSERT OR IGNORE INTO partner_seq(name, value) VALUES ('partner', 0)"
        )


# ----------------------------------------------------------------- 查询


def find_by_business_key(
    connection: sqlite3.Connection, mobile: str, id_card: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM partner WHERE mobile = ? AND upper(id_card) = ?",
        (mobile, id_card.upper()),
    ).fetchone()


def find_by_mobile(connection: sqlite3.Connection, mobile: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM partner WHERE mobile = ?", (mobile,)
    ).fetchone()


def find_by_id_card(connection: sqlite3.Connection, id_card: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM partner WHERE upper(id_card) = ?", (id_card.upper(),)
    ).fetchone()


def list_partners(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM partner ORDER BY seq").fetchall()
    return [dict(row) for row in rows]


def list_audit_logs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM partner_audit_log ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


# ----------------------------------------------------------------- 写入


def next_partner_sequence(connection: sqlite3.Connection) -> int:
    """在事务内递增注册序号，返回新序号。"""
    connection.execute(
        "UPDATE partner_seq SET value = value + 1 WHERE name = 'partner'"
    )
    row = connection.execute(
        "SELECT value FROM partner_seq WHERE name = 'partner'"
    ).fetchone()
    if row is None:  # pragma: no cover - schema 已保证存在
        raise RuntimeError("partner_seq 未初始化")
    return int(row["value"])


def insert_partner(
    connection: sqlite3.Connection,
    *,
    seq: int,
    partner_id: str,
    name: str,
    mobile: str,
    city: str,
    id_card: str,
    status: str,
    audit_status: str,
    audit_step: int,
    submit_at: str,
    idempotency_key: str,
    agreement_version: str,
    agreement_checked_at: str = "",
    agreement_ip: str = "",
    agreement_user_agent: str = "",
) -> None:
    """写入合伙人主档；这是 delta 演示里的「DB 插入变更」落点。"""
    connection.execute(
        """
        INSERT INTO partner (
            seq, partner_id, name, mobile, city, id_card, status,
            audit_status, audit_step, submit_at, idempotency_key,
            agreement_version, agreement_checked_at, agreement_ip,
            agreement_user_agent, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            seq,
            partner_id,
            name,
            mobile,
            city,
            id_card.upper(),
            status,
            audit_status,
            audit_step,
            submit_at,
            idempotency_key,
            agreement_version,
            agreement_checked_at,
            agreement_ip,
            agreement_user_agent,
            _now_iso(),
        ),
    )


def insert_audit_log(
    connection: sqlite3.Connection,
    *,
    partner_id: str,
    action: str,
    from_status: str | None,
    to_status: str,
    audit_step: int,
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO partner_audit_log (
            partner_id, action, from_status, to_status, audit_step, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (partner_id, action, from_status, to_status, audit_step, created_at),
    )


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
