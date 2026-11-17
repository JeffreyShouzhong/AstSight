"""数据库连接工厂：SQLite / PostgreSQL / MySQL。

造数执行与结构反射共用同一套连接参数；驱动按需导入，未安装时显式报
``db_driver_missing``，不静默降级。
"""
from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import unquote, urlsplit

from .dialects import DbTarget


class DbConnectError(RuntimeError):
    """驱动缺失、连接失败或目标不可用。"""


def connect(target: DbTarget):
    """按方言建立连接；调用方负责关闭。"""
    if target.dialect == "sqlite":
        path = target.sqlite_path
        if path is None:
            raise DbConnectError("sqlite 路径为空")
        if not path.is_file():
            raise DbConnectError(f"sqlite 数据库不存在: {path}")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    if target.dialect == "postgres":
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DbConnectError(
                "db_driver_missing: PostgreSQL 需要 psycopg，"
                "请安装 psycopg[binary]"
            ) from exc
        try:
            return psycopg.connect(target.url)
        except Exception as exc:  # 驱动异常类型随版本变化，统一转换
            raise DbConnectError(f"postgres 连接失败: {exc}") from exc
    if target.dialect == "mysql":
        try:
            import pymysql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DbConnectError(
                "db_driver_missing: MySQL 需要 PyMySQL，请安装 PyMySQL"
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
                raise DbConnectError(
                    "db_url_invalid: connect_timeout 必须是整数"
                ) from exc
        try:
            return pymysql.connect(**kwargs)
        except Exception as exc:
            raise DbConnectError(f"mysql 连接失败: {exc}") from exc
    raise DbConnectError(f"unsupported_dialect: {target.dialect!r}")


__all__ = ["DbConnectError", "connect"]
