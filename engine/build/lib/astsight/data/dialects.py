"""造数方言：URL 解析、标识符引用与占位符。

v1 支持 SQLite / PostgreSQL / MySQL。方言只影响 SQL 渲染与连接方式，
fixture 的语义与校验完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

SUPPORTED_DIALECTS = ("sqlite", "postgres", "mysql")

_ALIASES = {
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
}


class DialectError(ValueError):
    """URL 或方言不受支持。"""


def normalize_dialect(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    dialect = _ALIASES.get(raw)
    if dialect is None:
        raise DialectError(
            f"unsupported_dialect: 仅支持 {', '.join(SUPPORTED_DIALECTS)}，"
            f"实际 {raw or '<empty>'!r}"
        )
    return dialect


def _scheme_parts(db_url: str) -> tuple[str, str, str]:
    value = (db_url or "").strip()
    if "://" not in value:
        raise DialectError(f"db_url_invalid: 缺少 scheme: {value!r}")
    scheme, rest = value.split("://", 1)
    base, _, driver = scheme.partition("+")
    dialect = _ALIASES.get(base.lower())
    if dialect is None:
        raise DialectError(
            f"unsupported_dialect: 仅支持 {', '.join(SUPPORTED_DIALECTS)}，"
            f"实际 {base!r}"
        )
    if driver and dialect != "sqlite":
        # SQLAlchemy 风格的 postgresql+psycopg / mysql+pymysql；驱动由本模块选择。
        driver = driver.lower()
        allowed = (
            {"psycopg", "psycopg2"} if dialect == "postgres"
            else {"pymysql", "mysqldb"}
        )
        if driver not in allowed:
            raise DialectError(f"unsupported_driver: {scheme!r}")
    return dialect, rest, driver


def _require_database(dialect: str, rest: str) -> None:
    parts = urlsplit(f"{dialect}://{rest}")
    if not parts.path.strip("/"):
        raise DialectError(f"db_url_invalid: {dialect} 缺少数据库名")


def _redact(dialect: str, rest: str) -> str:
    parts = urlsplit(f"{dialect}://{rest}")
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{host}"
    return urlunsplit((dialect, netloc, parts.path, parts.query, ""))


@dataclass(frozen=True)
class DbTarget:
    dialect: str
    url: str
    display: str
    sqlite_path: Path | None = None
    options: dict[str, str] = field(default_factory=dict)


def parse_db_url(db_url: str) -> DbTarget:
    dialect, rest, _driver = _scheme_parts(db_url)
    if dialect == "sqlite":
        value = (db_url or "").strip()
        if not value.startswith("sqlite:///"):
            raise DialectError("db_url_invalid: sqlite 需要 sqlite:///path 形式")
        rest = value[len("sqlite:///") :]
        if not rest:
            raise DialectError("db_url_invalid: sqlite 路径为空")
        path = Path(rest)
        if str(path) in {":memory:", ""}:
            raise DialectError("不支持内存数据库：造数需要可回收的持久文件")
        resolved = path.resolve()
        return DbTarget(
            dialect="sqlite",
            url=f"sqlite:///{resolved.as_posix()}",
            display=str(resolved),
            sqlite_path=resolved,
        )

    _require_database(dialect, rest)
    parts = urlsplit(f"{dialect}://{rest}")
    options = {
        key: values[-1]
        for key, values in parse_qs(parts.query).items()
        if key in {"charset", "connect_timeout", "sslmode", "application_name"}
    }
    normalized = urlunsplit(
        (
            "postgresql" if dialect == "postgres" else "mysql",
            parts.netloc,
            parts.path,
            parts.query,
            "",
        )
    )
    return DbTarget(
        dialect=dialect,
        url=normalized,
        display=_redact(dialect, rest),
        options=options,
    )


def dialect_from_url(db_url: str) -> str:
    return parse_db_url(db_url).dialect


def placeholder(dialect: str) -> str:
    return "?" if normalize_dialect(dialect) == "sqlite" else "%s"


def quote_identifier(name: str, dialect: str = "sqlite") -> str:
    canonical = normalize_dialect(dialect)
    if canonical == "mysql":
        return f"`{name}`"
    return f'"{name}"'


__all__ = [
    "DbTarget",
    "DialectError",
    "SUPPORTED_DIALECTS",
    "dialect_from_url",
    "normalize_dialect",
    "parse_db_url",
    "placeholder",
    "quote_identifier",
]
