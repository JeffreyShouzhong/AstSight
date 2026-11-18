"""C6 用例编码：把 TestCase 编译为 pytest 代码资产。

生成约定（对齐能力文档 C6）：
- 每个接口一个测试文件，类=接口，方法=用例；
- docstring 带用例编号/类别/溯源；
- 生成物头部带生成器版本与时间；
- `# ASTSIGHT MANUAL BEGIN/END` 段在二次生成时保留；
- 生成后做语法自检，失败即报告，不把坏代码交给执行层。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from contracts import DataPlanBundle, TestCase, Transport

from ..artifacts import slugify, utc_now
from ..cases.data_refs import iter_data_refs, lookup_data, resolve_data_refs
from ..cases.expressions import field_paths, has_dynamic, has_relation_expression

GENERATOR = "AstSight M1 pytest-gen"

_CONFTEST = '''"""AstSight 生成物：HTTP 客户端与用例隔离。

手工修改请放在 MANUAL BEGIN/END 之间——二次生成会保留该段。
"""
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
import pytest

BASE_URL = os.environ.get("ASTSIGHT_BASE_URL", "http://127.0.0.1:8000")
RESET_URL = os.environ.get("ASTSIGHT_RESET_URL", "")
TIMEOUT = float(os.environ.get("ASTSIGHT_TIMEOUT", "10"))

MOCK_ADMIN_URL = os.environ.get("ASTSIGHT_MOCK_ADMIN_URL", "").rstrip("/")
MOCK_TOKEN = os.environ.get("ASTSIGHT_MOCK_TOKEN", "")
MOCK_NAMESPACE = os.environ.get("ASTSIGHT_MOCK_NAMESPACE", "")
MOCK_RUN_SCOPE = os.environ.get("ASTSIGHT_MOCK_RUN_SCOPE", "")
MOCK_DRAIN_MS = int(os.environ.get("ASTSIGHT_MOCK_DRAIN_MS", "200"))
MOCK_TTL_MS = int(os.environ.get("ASTSIGHT_MOCK_TTL_MS", "60000"))

DUBBO_URL = os.environ.get("ASTSIGHT_DUBBO_URL", "").strip()
DUBBO_HOST = os.environ.get("ASTSIGHT_DUBBO_HOST", "127.0.0.1").strip() or "127.0.0.1"
DUBBO_TARGET_ERROR = os.environ.get("ASTSIGHT_DUBBO_TARGET_ERROR", "").strip()
try:
    DUBBO_TARGETS = json.loads(
        os.environ.get("ASTSIGHT_DUBBO_TARGETS", "{}") or "{}"
    )
except ValueError:
    DUBBO_TARGETS = {}
if not isinstance(DUBBO_TARGETS, dict):
    DUBBO_TARGETS = {}


def _dubbo_service_key(interface, group, version):
    query = []
    if group:
        query.append(f"group={group}")
    if version:
        query.append(f"version={version}")
    return interface + ("?" + "&".join(query) if query else "")

DB_URL = os.environ.get("ASTSIGHT_DB_URL", "")
DB_POLL_MS = max(10, int(os.environ.get("ASTSIGHT_DB_POLL_MS", "50")))
DATA_PLAN_PATH = os.environ.get(
    "ASTSIGHT_DATA_PLAN_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_plan.json"),
)

_RESOLVED_NAMESPACE = None
_DATA_PLANS = None


def _mock_headers():
    return {"Authorization": f"Bearer {MOCK_TOKEN}"} if MOCK_TOKEN else {}


def _mock_namespace():
    global _RESOLVED_NAMESPACE
    if MOCK_NAMESPACE:
        return MOCK_NAMESPACE
    if _RESOLVED_NAMESPACE is None:
        health = httpx.get(
            f"{MOCK_ADMIN_URL}/_admin/health", headers=_mock_headers(), timeout=TIMEOUT
        )
        health.raise_for_status()
        _RESOLVED_NAMESPACE = str(health.json()["namespace"])
    return _RESOLVED_NAMESPACE


@contextmanager
def _open_mock_session(case_id, bindings):
    """用例级 Mock 会话：创建 → 执行 → drain → 关闭。

    未配置 ASTSIGHT_MOCK_ADMIN_URL 时退化为 no-op，保持纯接口用例可独立运行。
    drain 超时会抛错，避免在途调用落到下一个用例。
    """
    if not MOCK_ADMIN_URL:
        yield None
        return
    session_id = "astsight-" + uuid.uuid4().hex
    normalized_bindings = []
    for dep_id, value in bindings.items():
        if isinstance(value, str):
            item = {"dep_id": str(dep_id), "branch": str(value)}
        else:
            item = {"dep_id": str(dep_id), **value}
        normalized_bindings.append(item)
    payload = {
        "session_id": session_id,
        "namespace": _mock_namespace(),
        "case_id": case_id,
        "run_scope": MOCK_RUN_SCOPE,
        "bindings": normalized_bindings,
        "ttl_ms": MOCK_TTL_MS,
    }
    create = httpx.post(
        f"{MOCK_ADMIN_URL}/_admin/sessions",
        headers=_mock_headers(),
        json=payload,
        timeout=TIMEOUT,
    )
    if create.status_code != 200:
        raise RuntimeError(
            f"mock_session_create_failed: {create.status_code} {create.text[:500]}"
        )
    primary = None
    try:
        yield session_id
    except BaseException as exc:
        primary = exc
        raise
    finally:
        close_error = None
        try:
            drain = httpx.post(
                f"{MOCK_ADMIN_URL}/_admin/sessions/{session_id}/drain",
                headers=_mock_headers(),
                json={"timeout_ms": MOCK_DRAIN_MS},
                timeout=TIMEOUT,
            )
            if drain.status_code != 200:
                close_error = f"drain status={drain.status_code} {drain.text[:500]}"
            elif not drain.json().get("drained"):
                close_error = f"mock_session_not_drained: {drain.text[:500]}"
        except httpx.HTTPError as exc:
            close_error = f"mock_session_drain_failed: {exc}"
        try:
            delete = httpx.delete(
                f"{MOCK_ADMIN_URL}/_admin/sessions/{session_id}",
                headers=_mock_headers(),
                timeout=TIMEOUT,
            )
            if delete.status_code != 200 and close_error is None:
                close_error = f"delete status={delete.status_code} {delete.text[:500]}"
        except httpx.HTTPError as exc:
            if close_error is None:
                close_error = f"mock_session_close_failed: {exc}"
        # 用例本身失败时保留原始异常，不让清理错误掩盖真正的断言结果。
        if close_error is not None and primary is None:
            raise RuntimeError(close_error)


def _load_data_plans(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"data_plan_invalid: {exc}") from exc
    plans = payload.get("plans") if isinstance(payload, dict) else payload
    index = {}
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        for key in (plan.get("case_id"), plan.get("case_key")):
            if key:
                index[str(key)] = plan
    return index


def _data_plans():
    global _DATA_PLANS
    if _DATA_PLANS is None:
        _DATA_PLANS = _load_data_plans(DATA_PLAN_PATH)
    return _DATA_PLANS


def _db_dialect(db_url):
    value = str(db_url or "")
    scheme = value.split("://", 1)[0].lower().split("+", 1)[0]
    if scheme in {"sqlite", "sqlite3"}:
        return "sqlite"
    if scheme in {"postgres", "postgresql"}:
        return "postgres"
    if scheme in {"mysql", "mariadb"}:
        return "mysql"
    raise RuntimeError(f"unsupported_dialect: {scheme!r}")


def _connect_db(db_url):
    dialect = _db_dialect(db_url)
    if dialect == "sqlite":
        prefix = "sqlite:///"
        raw = db_url[len(prefix):] if db_url.startswith(prefix) else db_url.split("://", 1)[1]
        if not raw or raw == ":memory:":
            raise RuntimeError("unsupported_dialect: sqlite file path required")
        if not os.path.isfile(raw):
            raise RuntimeError(f"sqlite database not found: {raw}")
        conn = sqlite3.connect(raw)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn, dialect
    if dialect == "postgres":
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "db_driver_missing: PostgreSQL 造数需要 psycopg"
            ) from exc
        return psycopg.connect(db_url), dialect
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("db_driver_missing: MySQL 造数需要 PyMySQL") from exc
    parts = urlparse(db_url)
    options = parse_qs(parts.query)
    conn = pymysql.connect(
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 3306,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        database=unquote(parts.path.lstrip("/")),
        charset=(options.get("charset") or ["utf8mb4"])[-1],
        autocommit=False,
    )
    return conn, dialect


def _check_data_statement(statement, kind):
    if not isinstance(statement, dict) or statement.get("kind") != kind:
        raise RuntimeError(f"data_plan_invalid: expected {kind} statement")
    sql = str(statement.get("sql") or "")
    prefix = "INSERT INTO " if kind == "insert" else "DELETE FROM "
    if not sql.upper().startswith(prefix) or ("?" not in sql and "%s" not in sql):
        raise RuntimeError(f"data_plan_invalid: non-parameterized {kind}")
    if not isinstance(statement.get("params"), list):
        raise RuntimeError("data_plan_invalid: params must be list")


def _run_post_assertions(plan):
    """用例正常结束后执行迁移后只读断言；失败时照常进入 teardown。"""
    assertions = plan.get("post_assertions") or []
    if not assertions:
        return
    tools = _DbTools()
    try:
        for item in assertions:
            if not isinstance(item, dict):
                continue
            tools.check(
                item.get("table"),
                item.get("column"),
                item.get("op") or "eq",
                item.get("expect"),
                item.get("where") or {},
                int(item.get("wait_ms") or 0),
            )
    finally:
        tools.close()


@pytest.fixture()
def data_state(request):
    """用例级造数：请求前 apply，用例结束后 teardown。

    未声明前置状态的用例不受影响；声明了 preconditions 却没有 plan 或
    数据库连接时显式 skip 为 data_unavailable，不伪装成通过。
    """
    marker = request.node.get_closest_marker("astsight_data")
    if marker is None:
        yield {"status": "not_required", "applied": 0, "deleted": 0}
        return
    case_id = str(marker.args[0]) if marker.args else ""
    requires_data = bool(marker.kwargs.get("requires_data"))
    plan = _data_plans().get(case_id)
    record = {
        "case_id": case_id,
        "status": "not_required",
        "applied": 0,
        "deleted": 0,
    }
    if plan is None:
        if requires_data:
            pytest.skip(
                "data_unavailable: case declares preconditions but no data plan "
                f"was found for {case_id}"
            )
        yield record
        return
    if not DB_URL:
        pytest.skip(
            "data_unavailable: ASTSIGHT_DB_URL is not configured; "
            f"data plan for {case_id} was not applied"
        )

    conn = None
    try:
        plan_dialect = str(plan.get("dialect") or "sqlite")
        db_dialect = _db_dialect(DB_URL)
        if plan_dialect != db_dialect:
            raise RuntimeError(
                f"data_dialect_mismatch: plan={plan_dialect}, db={db_dialect}"
            )
        inserts = list(plan.get("statements") or [])
        teardown = plan.get("teardown")
        if not isinstance(teardown, list):
            raise RuntimeError("data_plan_invalid: teardown statements missing")
        for statement in inserts:
            _check_data_statement(statement, "insert")
        for statement in teardown:
            _check_data_statement(statement, "delete")
        conn, _ = _connect_db(DB_URL)
        cursor = conn.cursor()
        try:
            for statement in inserts:
                cursor.execute(statement["sql"], statement["params"])
                record["applied"] += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            cursor.close()
    except Exception as exc:
        if conn is not None:
            conn.close()
        pytest.skip(f"data_unavailable: {exc}")

    record["status"] = "applied"
    primary = None
    try:
        yield record
    except BaseException as exc:
        primary = exc
        raise
    else:
        _run_post_assertions(plan)
    finally:
        close_error = None
        try:
            cursor = conn.cursor()
            try:
                for statement in reversed(teardown):
                    cursor.execute(statement["sql"], statement["params"])
                    record["deleted"] += 1
                conn.commit()
            finally:
                cursor.close()
        except BaseException as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            close_error = exc
        finally:
            conn.close()
        if close_error is not None and primary is None:
            raise RuntimeError(f"data_teardown_failed: {close_error}")


def _resolve_step_refs(value, responses):
    """递归替换 {'$ref': 'step.field'}；引用缺失时让用例显式失败。"""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            ref = value["$ref"]
            if not isinstance(ref, str) or "." not in ref:
                raise AssertionError(f"invalid step ref: {ref!r}")
            step, _, field = ref.partition(".")
            if step not in responses:
                raise AssertionError(f"unknown step ref: {ref!r}")
            current = responses[step]
            for part in field.split("."):
                if not isinstance(current, dict) or part not in current:
                    raise AssertionError(f"missing step ref field: {ref!r}")
                current = current[part]
            return current
        return {
            key: _resolve_step_refs(item, responses) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_step_refs(item, responses) for item in value]
    return value


def _path_value(root, path, label="value"):
    current = root
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            raise AssertionError(f"missing {label} field: {path}")
        current = current[part]
    return current


def _expression_key(value):
    """count_distinct 的去重键：区分类型，字典/列表按 JSON 归一。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return (type(value).__name__, value)


def _projection_field(value):
    return (
        isinstance(value, str)
        and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value) is not None
    )


def _resolve_expression(value, responses, body, request_body):
    """解析断言右侧的受限表达式，不执行任意 Python。"""
    if isinstance(value, dict):
        if set(value) == {"$field"}:
            return _path_value(body, value["$field"], "response")
        if set(value) == {"$request"}:
            return _path_value(request_body, value["$request"], "request")
        if set(value) == {"$ref"}:
            return _resolve_step_refs(value, responses)
        if set(value) == {"$op", "args"}:
            op = value["$op"]
            args = [
                _resolve_expression(item, responses, body, request_body)
                for item in value["args"]
            ]
            try:
                if op == "len":
                    return len(args[0])
                if op == "sum":
                    return sum(args[0])
                if op == "avg":
                    items = args[0]
                    if not isinstance(items, (list, tuple)):
                        raise AssertionError("expression avg requires a list")
                    if not items:
                        raise AssertionError(
                            "expression avg requires a non-empty list"
                        )
                    return sum(items) / len(items)
                if op == "min":
                    items = args[0]
                    if not isinstance(items, (list, tuple)):
                        raise AssertionError("expression min requires a list")
                    if not items:
                        raise AssertionError(
                            "expression min requires a non-empty list"
                        )
                    return min(items)
                if op == "max":
                    items = args[0]
                    if not isinstance(items, (list, tuple)):
                        raise AssertionError("expression max requires a list")
                    if not items:
                        raise AssertionError(
                            "expression max requires a non-empty list"
                        )
                    return max(items)
                if op == "count_distinct":
                    items = args[0]
                    if not isinstance(items, (list, tuple)):
                        raise AssertionError(
                            "expression count_distinct requires a list"
                        )
                    return len({_expression_key(item) for item in items})
                if op == "pluck":
                    items, field = args
                    if not isinstance(items, (list, tuple)):
                        raise AssertionError(
                            "expression pluck requires a list"
                        )
                    if not _projection_field(field):
                        raise AssertionError(
                            f"expression pluck invalid field: {field!r}"
                        )
                    out = []
                    for index, item in enumerate(items):
                        if not isinstance(item, dict) or field not in item:
                            raise AssertionError(
                                f"expression pluck missing field {field!r} "
                                f"at index {index}"
                            )
                        out.append(item[field])
                    return out
                if op == "add":
                    return sum(args)
                if op == "sub":
                    return args[0] - args[1]
                if op == "mul":
                    result = args[0]
                    for item in args[1:]:
                        result *= item
                    return result
                if op == "div":
                    return args[0] / args[1]
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise AssertionError(f"expression {op} failed: {exc}") from exc
            raise AssertionError(f"unsupported expression op: {op!r}")
        return {
            key: _resolve_expression(item, responses, body, request_body)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_expression(item, responses, body, request_body)
            for item in value
        ]
    return value


def _render_step_path(path, params):
    def replace(match):
        name = match.group(1) or match.group(2) or match.group(3)
        if name not in params:
            return match.group(0)
        return quote(str(params[name]), safe="")

    pattern = r"\\{([^}/]+)\\}|<[^:>]+:([^>]+)>|<([^>]+)>"
    return re.sub(pattern, replace, path)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "astsight_data(case_id, requires_data=False): 用例级造数计划绑定",
    )


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL.rstrip("/")


@pytest.fixture()
def client(base_url: str):
    # 可选的用例隔离钩子：被测项目提供重置入口时才启用。
    # 这不是 AstSight 的必需依赖，而是让"有状态服务"的正向用例可重复执行。
    if RESET_URL:
        # 既支持路径（/_reset），也支持完整 URL——后者便于 Git Bash 等
        # 会改写 "/" 开头参数的 shell 直接传参。
        reset = RESET_URL if RESET_URL.startswith(("http://", "https://")) else base_url + RESET_URL
        resp = httpx.post(reset, timeout=TIMEOUT)
        resp.raise_for_status()
    with httpx.Client(base_url=base_url, timeout=TIMEOUT) as http:
        yield http


@pytest.fixture()
def mock_session():
    return _open_mock_session


class _StepTools:
    refs = staticmethod(_resolve_step_refs)
    expr = staticmethod(_resolve_expression)
    path = staticmethod(_render_step_path)


@pytest.fixture()
def step_tools():
    return _StepTools()


def _db_quote(name, dialect):
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name or ""):
        raise RuntimeError(f"db_assert_invalid: identifier {name!r}")
    return f"`{name}`" if dialect == "mysql" else f'"{name}"'


class _DbTools:
    """只读 db_row 断言；连接按需建立，未配置 ASTSIGHT_DB_URL 时显式 skip。"""

    def __init__(self):
        self._conn = None
        self._dialect = None

    def _connection(self):
        if not DB_URL:
            pytest.skip(
                "data_unavailable: ASTSIGHT_DB_URL is not configured; "
                "db assertion was not executed"
            )
        if self._conn is None:
            self._conn, self._dialect = _connect_db(DB_URL)
        return self._conn, self._dialect

    def check(self, table, column, op, expect, where, wait_ms=0):
        wait_ms = max(0, int(wait_ms or 0))
        deadline = time.monotonic() + wait_ms / 1000.0
        while True:
            try:
                self._check_once(table, column, op, expect, where)
                return
            except AssertionError as exc:
                if wait_ms <= 0 or time.monotonic() >= deadline:
                    if wait_ms:
                        raise AssertionError(f"{exc} (waited {wait_ms} ms)") from exc
                    raise
                # 结束当前只读事务，让 MySQL REPEATABLE READ 下一轮看到新提交。
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                time.sleep(min(DB_POLL_MS / 1000.0, max(0, deadline - time.monotonic())))

    def _check_once(self, table, column, op, expect, where):
        conn, dialect = self._connection()
        marker = "?" if dialect == "sqlite" else "%s"
        conditions = []
        params = []
        for name, value in (where or {}).items():
            conditions.append(f"{_db_quote(name, dialect)} = {marker}")
            params.append(value)
        where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        table_sql = _db_quote(table, dialect)
        cursor = conn.cursor()
        try:
            if op == "count_eq":
                cursor.execute(
                    f"SELECT COUNT(*) FROM {table_sql}{where_sql}", params
                )
                actual = cursor.fetchone()[0]
                assert actual == expect, (
                    f"db_row expected count {expect}, got {actual}"
                )
                return
            select = "1" if not column else _db_quote(column, dialect)
            cursor.execute(
                f"SELECT {select} FROM {table_sql}{where_sql} LIMIT 1", params
            )
            row = cursor.fetchone()
            if op == "exists":
                assert row is not None, "db_row expected a row, got none"
            elif op == "absent":
                assert row is None, f"db_row expected no row, got {row!r}"
            elif op == "eq":
                assert row is not None and row[0] == expect, (
                    f"db_row expected {expect!r}, got {row!r}"
                )
            elif op == "ne":
                assert row is not None and row[0] != expect, (
                    f"db_row expected value != {expect!r}, got {row!r}"
                )
            elif op == "gte":
                assert row is not None and row[0] >= expect, (
                    f"db_row expected >= {expect!r}, got {row!r}"
                )
            elif op == "lte":
                assert row is not None and row[0] <= expect, (
                    f"db_row expected <= {expect!r}, got {row!r}"
                )
            else:
                raise RuntimeError(f"db_assert_invalid: op {op!r}")
        finally:
            cursor.close()

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class _SchemaTools:
    def check(self, body, schema):
        try:
            import jsonschema
        except ImportError as exc:
            raise RuntimeError(
                "schema_validation_unavailable: 请安装 jsonschema"
            ) from exc
        try:
            jsonschema.validate(instance=body, schema=schema)
        except jsonschema.ValidationError as exc:
            location = "/".join(str(part) for part in exc.path) or "<root>"
            raise AssertionError(
                f"response schema mismatch at {location}: {exc.message}"
            ) from exc


@pytest.fixture()
def db_tools():
    tools = _DbTools()
    try:
        yield tools
    finally:
        tools.close()


@pytest.fixture()
def schema_tools():
    return _SchemaTools()


class _DubboTools:
    @staticmethod
    def field(value, path):
        current = value
        for part in str(path).split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, (list, tuple)):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current


@pytest.fixture(scope="session")
def dubbo_client():
    from astsight.sidecar import DubboSidecar, DubboSidecarError

    try:
        client = DubboSidecar().start()
    except DubboSidecarError as exc:
        pytest.skip(f"dubbo_sidecar_unavailable: {exc.code}: {exc}")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def dubbo_call(request):
    def call(*, url=None, target_port=None, **kwargs):
        interface = str(kwargs.get("interface") or "")
        group = kwargs.get("group")
        version = kwargs.get("version")
        service_key = _dubbo_service_key(interface, group, version)
        if DUBBO_TARGET_ERROR:
            # 保留原始错误码（来源不一致、映射冲突等），执行层按 blocked 归类。
            error_code = DUBBO_TARGET_ERROR.split(":", 1)[0].strip()
            pytest.skip(f"{error_code or 'dubbo_target_unresolved'}: {DUBBO_TARGET_ERROR}")
        target = str(url or "").strip()
        if not target and DUBBO_TARGETS:
            # 多服务严格映射：命中不了就阻断，不拿全局 URL 猜一个。
            target = str(
                DUBBO_TARGETS.get(service_key)
                or DUBBO_TARGETS.get(interface)
                or ""
            )
            if not target:
                pytest.skip(f"dubbo_target_unresolved: {service_key}")
        if not target:
            target = DUBBO_URL
        if not target and target_port:
            serialization = str(kwargs.get("serialization") or "").lower()
            scheme = "tri" if serialization in {"protobuf", "proto"} else "dubbo"
            target = f"{scheme}://{DUBBO_HOST}:{int(target_port)}"
        if not target:
            pytest.skip(
                "dubbo_target_unresolved: 设置 ASTSIGHT_DUBBO_URL、"
                "ASTSIGHT_DUBBO_TARGETS 或 ASTSIGHT_DUBBO_HOST；不回退成 HTTP"
            )
        scheme = target.partition("://")[0].lower()
        serialization = str(kwargs.get("serialization") or "").lower()
        if (
            scheme in {"tri", "grpc"}
            and kwargs.get("descriptor_source")
            and serialization in {"protobuf", "proto"}
        ):
            from astsight.sidecar.triple import invoke_triple

            return invoke_triple(url=target, **kwargs)
        dubbo_client = request.getfixturevalue("dubbo_client")
        return dubbo_client.invoke(url=target, **kwargs)

    return call


@pytest.fixture()
def dubbo_tools():
    return _DubboTools()


# ASTSIGHT MANUAL BEGIN
# ASTSIGHT MANUAL END
'''

_PATH_PARAM_RE = re.compile(r"\{([^}/]+)\}|<[^:>]+:([^>]+)>|<([^>]+)>")


@dataclass
class CodegenResult:
    files: list[Path] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files": [str(p) for p in self.files],
            "errors": self.errors,
            "skipped": self.skipped,
            "generator": GENERATOR,
            "generated_at": utc_now(),
        }


def _render_path(path: str, path_params: dict) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1) or match.group(2) or match.group(3)
        if name not in path_params:
            return match.group(0)
        return quote(str(path_params[name]), safe="")

    return _PATH_PARAM_RE.sub(replace, path)


def _value_has_ref(value: Any) -> bool:
    if isinstance(value, dict):
        if "$ref" in value:
            return True
        return any(_value_has_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_value_has_ref(item) for item in value)
    return False


def _ref_steps(value: Any) -> list[str]:
    """收集值中 ``$ref`` 指向的步骤名。"""
    out: list[str] = []
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            out.append(value["$ref"].partition(".")[0])
            return out
        for item in value.values():
            out.extend(_ref_steps(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_ref_steps(item))
    return out


def _expect_expr(
    expect: Any,
    responses_expr: str,
    *,
    body_var: str | None = None,
    request_expr: str | None = None,
) -> str:
    if has_relation_expression(expect):
        return (
            f"step_tools.expr({expect!r}, {responses_expr}, "
            f"{body_var or 'None'}, {request_expr or 'None'})"
        )
    if has_dynamic(expect):
        return f"step_tools.refs({expect!r}, {responses_expr})"
    return repr(expect)


def _assert_line(
    assertion,
    *,
    resp_var: str = "resp",
    body_var: str = "body",
    responses_expr: str = "{}",
    request_expr: str | None = None,
) -> str:
    kind = assertion.kind
    op = assertion.op
    expect = assertion.expect
    uses_response_body = kind == "body_field" or (
        kind == "db_row"
        and bool(field_paths(expect) or field_paths(assertion.where))
    )
    expect_expr = _expect_expr(
        expect,
        responses_expr,
        body_var=body_var if uses_response_body else None,
        request_expr=request_expr,
    )
    if kind == "status_code":
        if op == "in":
            return (
                f"        assert {resp_var}.status_code in {expect!r}, "
                f"f\"expected status in {expect!r}, got {{{resp_var}.status_code}}: "
                f"{{{resp_var}.text[:500]}}\""
            )
        if op == "between":
            low, high = expect
            return (
                f"        assert {low} <= {resp_var}.status_code <= {high}, "
                f"f\"expected status in [{low}, {high}], got {{{resp_var}.status_code}}: "
                f"{{{resp_var}.text[:500]}}\""
            )
        return (
            f"        assert {resp_var}.status_code == {expect!r}, "
            f"f\"expected status {expect!r}, got {{{resp_var}.status_code}}: "
            f"{{{resp_var}.text[:500]}}\""
        )

    if kind == "body_field":
        target = assertion.target or ""
        if op == "exists":
            return (
                f"        assert {target!r} in {body_var}, "
                f"f\"missing response field {target}\""
            )
        if op == "eq":
            return (
                f"        assert {body_var}.get({target!r}) == {expect_expr}, "
                f"f\"unexpected value for {target}: "
                f"{{{body_var}.get({target!r})!r}}\""
            )
        if op == "ne":
            return f"        assert {body_var}.get({target!r}) != {expect_expr}"
        if op == "in":
            return f"        assert {body_var}.get({target!r}) in {expect_expr}"
        if op == "gte":
            return f"        assert {body_var}.get({target!r}) >= {expect_expr}"
        if op == "lte":
            return f"        assert {body_var}.get({target!r}) <= {expect_expr}"
        if op == "contains":
            return (
                f"        assert {expect_expr} in "
                f"str({body_var}.get({target!r}))"
            )
    if kind == "header":
        target = assertion.target or ""
        if op == "exists":
            return f"        assert {target!r} in {resp_var}.headers"
        if op == "contains":
            return (
                f"        assert {expect_expr} in "
                f"str({resp_var}.headers.get({target!r}, \"\"))"
            )
        return (
            f"        assert {resp_var}.headers.get({target!r}) == {expect_expr}"
        )
    if kind == "body_schema":
        return f"        schema_tools.check({body_var}, {expect!r})"
    if kind == "db_row":
        table, _, column = (assertion.target or "").partition(".")
        where_expr = _expect_expr(
            assertion.where or {},
            responses_expr,
            body_var=body_var if uses_response_body else None,
            request_expr=request_expr,
        )
        wait_ms = int(getattr(assertion, "wait_ms", 0) or 0)
        return (
            f"        db_tools.check({table!r}, {column or None!r}, "
            f"{op!r}, {expect_expr}, {where_expr}, wait_ms={wait_ms})"
        )
    return f"        # unsupported assertion: {kind}/{op}"


def _dubbo_assert_line(
    assertion,
    *,
    result_var: str = "result",
    responses_expr: str = "{}",
    request_expr: str | None = None,
) -> str:
    kind = assertion.kind
    op = assertion.op
    expect = assertion.expect
    if kind == "rpc_status":
        if op == "in":
            return (
                f"        assert {result_var}.rpc_status in {expect!r}, "
                f"f\"unexpected rpc_status {{{result_var}.rpc_status}}\""
            )
        if op == "ne":
            return (
                f"        assert {result_var}.rpc_status != {expect!r}, "
                f"f\"unexpected rpc_status {{{result_var}.rpc_status}}\""
            )
        return (
            f"        assert {result_var}.rpc_status == {expect!r}, "
            f"f\"expected rpc_status {expect!r}, got "
            f"{{{result_var}.rpc_status}}: {{{result_var}.message}}\""
        )
    if kind == "result_present":
        if op == "absent":
            return f"        assert {result_var}.value is None, \"expected no result value\""
        return f"        assert {result_var}.value is not None, \"expected a result value\""
    if kind == "result_field":
        target = assertion.target or ""
        expr = f"dubbo_tools.field({result_var}.value, {target!r})"
        expect_expr = _expect_expr(
            expect,
            responses_expr,
            body_var=None,
            request_expr=request_expr,
        )
        if op == "exists":
            return (
                f"        assert {expr} is not None, "
                f"f\"missing result field {target}\""
            )
        if op == "eq":
            return (
                f"        assert {expr} == {expect_expr}, "
                f"f\"unexpected value for {target}: {{{expr}!r}}\""
            )
        if op == "ne":
            return f"        assert {expr} != {expect_expr}"
        if op == "in":
            return f"        assert {expr} in {expect_expr}"
        if op == "gte":
            return f"        assert {expr} >= {expect_expr}"
        if op == "lte":
            return f"        assert {expr} <= {expect_expr}"
        if op == "contains":
            return f"        assert {expect_expr} in str({expr})"
    if kind in {"error_code", "exception_type", "error_message"}:
        attr = {
            "error_code": "error",
            "exception_type": "exception_type",
            "error_message": "message",
        }[kind]
        expr = f"{result_var}.{attr}"
        if op == "contains":
            return f"        assert {expect!r} in str({expr} or '')"
        if op == "in":
            return f"        assert {expr} in {expect!r}"
        if op == "ne":
            return f"        assert {expr} != {expect!r}"
        return (
            f"        assert {expr} == {expect!r}, "
            f"f\"unexpected {kind}: {{{expr}!r}}\""
        )
    if kind == "db_row":
        table, _, column = (assertion.target or "").partition(".")
        wait_ms = int(getattr(assertion, "wait_ms", 0) or 0)
        expect_expr = _expect_expr(
            assertion.expect,
            responses_expr,
            body_var=None,
            request_expr=request_expr,
        )
        where_expr = _expect_expr(
            assertion.where or {},
            responses_expr,
            body_var=None,
            request_expr=request_expr,
        )
        return (
            f"        db_tools.check({table!r}, {column or None!r}, "
            f"{op!r}, {expect_expr}, {where_expr}, wait_ms={wait_ms})"
        )
    return f"        # unsupported assertion: {kind}/{op}"


def _dubbo_invoke_lines(
    call,
    *,
    result_var: str,
    responses_expr: str = "{}",
) -> list[str]:
    arguments_expr = _expect_expr(
        call.arguments, responses_expr, body_var=None
    )
    return [
        f"        {result_var} = dubbo_call(",
        f"            interface={call.interface!r},",
        f"            method={call.method!r},",
        f"            param_types={call.parameter_types!r},",
        f"            arguments={arguments_expr},",
        f"            url={call.url!r},",
        f"            target_port={call.target_port!r},",
        f"            group={call.group!r},",
        f"            version={call.version!r},",
        f"            attachments={call.attachments!r},",
        f"            timeout_ms={int(call.timeout_ms)},",
        f"            serialization={call.serialization!r},",
        f"            descriptor_source={call.descriptor_source!r},",
        f"            grpc_path={call.grpc_path!r},",
        f"            compatibility={call.compatibility!r},",
        "        )",
    ]


def _dubbo_case_lines(case: TestCase) -> list[str]:
    call = case.dubbo
    if call is None:
        return ["        raise RuntimeError('dubbo_call_missing')"]
    lines = _dubbo_invoke_lines(call, result_var="result")
    for assertion in case.asserts:
        lines.append(_dubbo_assert_line(assertion))
    return lines


def _responses_expr(case: TestCase, index: int, needed: set[str]) -> str:
    items = [
        f"{step.name!r}: body_{step.name}"
        for step in case.steps[:index]
        if step.name in needed
    ]
    return "{" + ", ".join(items) + "}"


def _needs_response_body(assertion: Any) -> bool:
    if assertion.kind in {"body_field", "body_schema"}:
        return True
    if assertion.kind != "db_row":
        return False
    return bool(
        field_paths(assertion.expect) or field_paths(assertion.where)
    )


def _request_args(
    *,
    method: str,
    path: str,
    path_params: dict[str, Any],
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any,
    responses_expr: str,
) -> list[str]:
    if _value_has_ref(path_params):
        route = (
            f"step_tools.path({path!r}, "
            f"step_tools.refs({path_params!r}, {responses_expr}))"
        )
    else:
        route = repr(_render_path(path, path_params))
    args = [repr(method), route]
    if query:
        args.append(
            f"params={_expect_expr(query, responses_expr, body_var=None)}"
        )
    if headers:
        args.append(f"headers={headers!r}")
    if body is not None:
        args.append(f"json={_expect_expr(body, responses_expr, body_var=None)}")
    return args


def _single_request_lines(case: TestCase) -> list[str]:
    request_expr = (
        _expect_expr(case.body, "{}", body_var=None)
        if case.body is not None
        else "None"
    )
    request_args = _request_args(
        method=case.method,
        path=case.path,
        path_params=case.path_params,
        query=case.query,
        headers=case.headers,
        body=case.body,
        responses_expr="{}",
    )
    # 先判状态码再解析 body：服务返回 500/非 JSON 时，失败信息应指向真正的
    # 问题（状态码），而不是被 JSONDecodeError 掩盖成一条无上下文的异常。
    inner = [f"        resp = client.request({', '.join(request_args)})"]
    body_asserts = [a for a in case.asserts if _needs_response_body(a)]
    for assertion in case.asserts:
        if not _needs_response_body(assertion):
            inner.append(_assert_line(assertion, request_expr=request_expr))
    if body_asserts:
        inner.append("        body = resp.json()")
        for assertion in body_asserts:
            inner.append(
                _assert_line(assertion, request_expr=request_expr)
            )
    return inner


def _steps_lines(case: TestCase) -> list[str]:
    needed: set[str] = set()
    for step in case.steps:
        for value in (step.body, step.query, step.path_params):
            needed.update(_ref_steps(value))
        if step.dubbo is not None:
            needed.update(_ref_steps(step.dubbo.arguments))
        for assertion in step.asserts:
            if assertion.kind == "body_schema":
                continue
            needed.update(_ref_steps(assertion.expect))
            if assertion.kind == "db_row":
                needed.update(_ref_steps(assertion.where))
    inner: list[str] = []
    for index, step in enumerate(case.steps):
        responses_expr = _responses_expr(case, index, needed)
        if step.dubbo is not None:
            resp_var = f"result_{step.name}"
            body_var = f"body_{step.name}"
            inner.extend(
                _dubbo_invoke_lines(
                    step.dubbo,
                    result_var=resp_var,
                    responses_expr=responses_expr,
                )
            )
            inner.append(f"        {body_var} = {resp_var}.value")
            for assertion in step.asserts:
                inner.append(
                    _dubbo_assert_line(
                        assertion,
                        result_var=resp_var,
                        responses_expr=responses_expr,
                    )
                )
            continue
        resp_var = f"resp_{step.name}"
        body_var = f"body_{step.name}"
        request_expr = (
            _expect_expr(step.body, responses_expr, body_var=None)
            if step.body is not None
            else "None"
        )
        request_args = _request_args(
            method=step.method,
            path=step.path,
            path_params=step.path_params,
            query=step.query,
            headers=step.headers,
            body=step.body,
            responses_expr=responses_expr,
        )
        inner.append(
            f"        {resp_var} = client.request({', '.join(request_args)})"
        )
        body_asserts = [
            assertion for assertion in step.asserts
            if _needs_response_body(assertion)
        ]
        for assertion in step.asserts:
            if not _needs_response_body(assertion):
                inner.append(
                    _assert_line(
                        assertion,
                        resp_var=resp_var,
                        responses_expr=responses_expr,
                        request_expr=request_expr,
                    )
                )
        if body_asserts or step.name in needed:
            inner.append(f"        {body_var} = {resp_var}.json()")
        for assertion in body_asserts:
            inner.append(
                _assert_line(
                    assertion,
                    resp_var=resp_var,
                    body_var=body_var,
                    responses_expr=responses_expr,
                    request_expr=request_expr,
                )
            )
    return inner


def _case_assertions(case: TestCase) -> list[Any]:
    return [
        *case.asserts,
        *(assertion for step in case.steps for assertion in step.asserts),
    ]


def _case_data_refs(case: TestCase) -> list[str]:
    values: list[Any] = [
        case.path_params,
        case.query,
        case.body,
        case.mock_replies,
    ]
    if case.dubbo is not None:
        values.extend([case.dubbo.arguments, case.dubbo.attachments])
    for step in case.steps:
        values.extend([step.path_params, step.query, step.body])
        if step.dubbo is not None:
            values.extend([step.dubbo.arguments, step.dubbo.attachments])
        for assertion in step.asserts:
            values.extend([assertion.expect, assertion.where])
    for assertion in case.asserts:
        values.extend([assertion.expect, assertion.where])
    refs: list[str] = []
    for value in values:
        refs.extend(iter_data_refs(value))
    return refs


def _unresolved_data_refs(case: TestCase) -> list[str]:
    return [
        ref for ref in _case_data_refs(case)
        if not lookup_data(case.data, ref)[0]
    ]


def _resolve_assertion(assertion, data):
    return assertion.model_copy(
        update={
            "expect": resolve_data_refs(assertion.expect, data),
            "where": resolve_data_refs(assertion.where, data),
        }
    )


def _resolve_step(step, data):
    return step.model_copy(
        update={
            "path_params": resolve_data_refs(step.path_params, data),
            "query": resolve_data_refs(step.query, data),
            "body": resolve_data_refs(step.body, data),
            "dubbo": _resolve_call(step.dubbo, data),
            "asserts": [
                _resolve_assertion(assertion, data)
                for assertion in step.asserts
            ],
        }
    )


def _resolve_call(call, data):
    if call is None:
        return None
    return call.model_copy(
        update={
            "arguments": resolve_data_refs(call.arguments, data),
            "attachments": resolve_data_refs(call.attachments, data),
        }
    )


def _resolve_case_data(case: TestCase) -> TestCase:
    """把 `$data` 引用换成实体值，生成物里只留确定性字面量。"""
    if not case.data:
        return case
    data = case.data
    return case.model_copy(
        update={
            "path_params": resolve_data_refs(case.path_params, data),
            "query": resolve_data_refs(case.query, data),
            "body": resolve_data_refs(case.body, data),
            "dubbo": _resolve_call(case.dubbo, data),
            "asserts": [
                _resolve_assertion(assertion, data) for assertion in case.asserts
            ],
            "steps": [_resolve_step(step, data) for step in case.steps],
            "mock_replies": resolve_data_refs(case.mock_replies, data),
        }
    )


def _case_function(case: TestCase, *, has_data_plan: bool = False) -> str:
    case = _resolve_case_data(case)
    name = f"test_{case.id.replace('-', '_').lower()}_{case.category}"
    bindings = _case_mock_bindings(case)
    requires_data = bool(case.preconditions)
    assertions = _case_assertions(case)
    is_dubbo = case.transport is Transport.DUBBO
    has_dubbo_step = any(step.dubbo is not None for step in case.steps)
    has_http_step = any(step.dubbo is None for step in case.steps)
    if case.steps:
        # 多步骤统一需要 step_tools（$ref/路径解析）；Dubbo 步骤也用它解析参数。
        fixtures = ["mock_session", "data_state", "step_tools"]
        if has_http_step:
            fixtures.append("client")
        if has_dubbo_step:
            fixtures.extend(["dubbo_call", "dubbo_tools"])
    else:
        fixtures = (
            ["mock_session", "data_state", "dubbo_call", "dubbo_tools"]
            if is_dubbo
            else ["client", "mock_session", "data_state", "step_tools"]
        )
    if any(assertion.kind == "db_row" for assertion in assertions):
        fixtures.append("db_tools")
    if any(assertion.kind == "body_schema" for assertion in assertions):
        fixtures.append("schema_tools")
    lines: list[str] = []
    if requires_data or has_data_plan:
        # 有前置状态或已生成 plan 的用例必须走 data_state fixture：
        # 缺 DB/plan 时它会显式 skip 为 data_unavailable，而不是静默通过。
        lines.append(
            f"    @pytest.mark.astsight_data({case.id!r}, "
            f"requires_data={requires_data!r})"
        )
    lines.extend(
        [
            f"    def {name}(self, {', '.join(fixtures)}):",
            f'        """case={case.id} endpoint={case.endpoint} category={case.category} '
            f'source={case.source}"""',
            # 未配置 Mock 时 fixture 是 no-op；配置后每个用例都有独立会话与证据。
            f"        with mock_session({case.id!r}, {bindings!r}):",
        ]
    )
    if case.steps:
        inner = _steps_lines(case)
    elif is_dubbo:
        inner = _dubbo_case_lines(case)
    else:
        inner = _single_request_lines(case)
    lines.extend("    " + line for line in inner)
    return "\n".join(lines)


def _case_mock_bindings(case: TestCase) -> dict[str, Any]:
    bindings: dict[str, Any] = {
        dep_id: {"branch": branch}
        for dep_id, branch in sorted(case.mock_bindings.items())
    }
    for dep_id in bindings:
        reply = case.mock_replies.get(dep_id)
        if reply is not None:
            bindings[dep_id]["reply"] = reply
        rules = [
            rule.model_dump(mode="json")
            for rule in case.mock_rules
            if rule.dep_id == dep_id
        ]
        sequence = list(case.mock_sequences.get(dep_id) or [])
        if rules:
            bindings[dep_id]["rules"] = [
                {"branch": rule["branch"], "match": rule["match"]}
                for rule in rules
            ]
        if sequence:
            bindings[dep_id]["sequence"] = sequence
    return bindings


def _class_name(method: str, path: str, used: set[str]) -> str:
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", path) if p]
    base = "Test" + method.title() + "".join(p.title() for p in parts)
    name, index = base, 2
    while name in used:
        name = f"{base}{index}"
        index += 1
    used.add(name)
    return name


def _manual_block(existing: str) -> str:
    match = re.search(
        r"# ASTSIGHT MANUAL BEGIN(.*?)# ASTSIGHT MANUAL END", existing, re.S
    )
    return match.group(1).strip("\n") if match else ""


def _plan_index(bundle: DataPlanBundle | None) -> dict[str, Any]:
    """case_id / case_key → DataPlan；两者都缺失的 plan 不参与匹配。"""
    index: dict[str, Any] = {}
    if bundle is None:
        return index
    for plan in bundle.plans:
        for key in (plan.case_id, plan.case_key):
            if key:
                index.setdefault(str(key), plan)
    return index


def _case_plan(
    case: TestCase, index: dict[str, Any]
) -> Any | None:
    return index.get(case.id) or index.get(getattr(case, "case_key", "") or "")


def generate_pytest(
    cases: list[TestCase],
    output_dir: str | Path,
    *,
    generated_at: str | None = None,
    data_bundle: DataPlanBundle | None = None,
    project_root: str | Path | None = None,
) -> CodegenResult:
    """生成 conftest.py + 每接口一个测试文件，并做语法自检。

    `data_bundle` 提供时，只把本次运行用例的 plan 写入 `data_plan.json`；
    conftest 的 `data_state` fixture 在请求前 apply、用例结束后 teardown。
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = CodegenResult()
    stamp = generated_at or utc_now()
    plan_index = _plan_index(data_bundle)
    if project_root is not None:
        cases = _resolve_case_descriptors(cases, Path(project_root))
    valid_cases: list[TestCase] = []
    for case in cases:
        unresolved = _unresolved_data_refs(case)
        if unresolved:
            result.errors.append(
                {
                    "endpoint": case.endpoint,
                    "case_id": case.id,
                    "error": "data_ref_unresolved: " + ", ".join(unresolved[:5]),
                }
            )
            continue
        valid_cases.append(case)
    cases = valid_cases

    conftest = out / "conftest.py"
    existing = conftest.read_text(encoding="utf-8") if conftest.exists() else ""
    manual = _manual_block(existing)
    content = _CONFTEST.replace("# ASTSIGHT MANUAL BEGIN\n# ASTSIGHT MANUAL END",
                                f"# ASTSIGHT MANUAL BEGIN\n{manual}\n# ASTSIGHT MANUAL END")
    conftest.write_text(content, encoding="utf-8")
    result.files.append(conftest)

    if data_bundle is not None:
        case_ids = {case.id for case in cases}
        case_keys = {
            getattr(case, "case_key", "") for case in cases if getattr(case, "case_key", "")
        }
        matched = [
            plan
            for plan in data_bundle.plans
            if (plan.case_id and plan.case_id in case_ids)
            or (plan.case_key and plan.case_key in case_keys)
        ]
        filtered = data_bundle.model_copy(update={"plans": matched})
        plan_file = out / "data_plan.json"
        plan_file.write_text(
            json.dumps(filtered.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result.files.append(plan_file)

    grouped: dict[str, list[TestCase]] = {}
    for case in cases:
        grouped.setdefault(case.endpoint, []).append(case)

    used_classes: set[str] = set()
    for endpoint, endpoint_cases in grouped.items():
        first = endpoint_cases[0]
        is_dubbo_case = (
            first.transport is Transport.DUBBO or endpoint.startswith("dubbo:")
        )
        if is_dubbo_case:
            identifier = (
                f"{first.dubbo.interface}_{first.dubbo.method}"
                if first.dubbo is not None
                else endpoint.partition("dubbo:")[2] or endpoint
            )
            class_name = _class_name("Dubbo", identifier, used_classes)
            # 文件名不能含 `.`，否则 pytest 会把它当成包路径。
            filename = f"test_dubbo_{slugify(identifier.replace('.', '_'))}.py"
        else:
            method, _, path = endpoint.partition(" ")
            class_name = _class_name(method, path, used_classes)
            filename = f"test_{slugify(method.lower() + '_' + path)}.py"
        target = out / filename
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        manual = _manual_block(existing)

        header = (
            f'"""Auto-generated by {GENERATOR} at {stamp}.\n\n'
            f"Endpoint: {endpoint}\n"
            f"Cases: {len(endpoint_cases)}\n"
            f'Manual: 手工修改放在 ASTSIGHT MANUAL 段内。\n"""\n'
            f"import pytest\n\n\n"
        )
        body = "\n\n".join(
            _case_function(c, has_data_plan=_case_plan(c, plan_index) is not None)
            for c in endpoint_cases
        )
        footer = f"\n\n# ASTSIGHT MANUAL BEGIN\n{manual}\n# ASTSIGHT MANUAL END\n"
        source = f"{header}class {class_name}:\n{body}{footer}"

        try:
            compile(source, str(target), "exec")
        except SyntaxError as exc:
            result.errors.append(
                {"endpoint": endpoint, "error": f"{exc.msg} (line {exc.lineno})"}
            )
            continue

        target.write_text(source, encoding="utf-8")
        result.files.append(target)

    return result


def _resolve_case_descriptors(
    cases: list[TestCase],
    project_root: Path,
) -> list[TestCase]:
    """把自动画像中的相对 descriptor 固化到项目根，生成代码可脱离 cwd。"""
    root = project_root.expanduser().resolve()

    def normalized(call):
        if call is None or not call.descriptor_source:
            return call
        descriptor = Path(call.descriptor_source).expanduser()
        if descriptor.is_absolute():
            return call
        return call.model_copy(
            update={"descriptor_source": str((root / descriptor).resolve())}
        )

    result: list[TestCase] = []
    for case in cases:
        updates: dict[str, Any] = {}
        if case.dubbo is not None:
            updates["dubbo"] = normalized(case.dubbo)
        if case.steps:
            updates["steps"] = [
                step.model_copy(update={"dubbo": normalized(step.dubbo)})
                if step.transport is Transport.DUBBO and step.dubbo is not None
                else step
                for step in case.steps
            ]
        result.append(case.model_copy(update=updates) if updates else case)
    return result
