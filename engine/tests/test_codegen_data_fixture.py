"""C6 造数 fixture：请求前 apply、结束后 teardown、缺 DB 标 blocked。"""
from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astsight.codegen import generate_pytest
from astsight.data import build_data_plan_bundle
from astsight.execute.runner import collect_only, run_pytest
from contracts import (
    Assertion,
    ColumnCatalog,
    DataCatalog,
    DataState,
    Evidence,
    Precondition,
    PreconditionKind,
    TableCatalog,
    TestCase as CaseModel,
)


def _case() -> CaseModel:
    return CaseModel(
        id="TC-0001",
        endpoint="GET /probe",
        category="positive",
        method="GET",
        path="/probe",
        source="test",
        asserts=[
            Assertion(kind="status_code", op="eq", expect=200),
            Assertion(kind="body_field", target="count", op="eq", expect=1),
        ],
        preconditions=[
            Precondition(
                kind=PreconditionKind.DB_STATE,
                state_key="items_ready",
                evidence=Evidence.VERIFIED,
            )
        ],
    )


def _catalog() -> DataCatalog:
    return DataCatalog(
        schema_version=1,
        catalog_id="cat-1",
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
        states=[
            DataState(
                state_key="items_ready",
                table_values={"items": [{"id": 1, "name": "T-run-item"}]},
                evidence=Evidence.VERIFIED,
            )
        ],
    )


def _bundle(case: CaseModel):
    bundle = build_data_plan_bundle(
        cases=[case],
        catalog=_catalog(),
        run_scope="t1",
    )
    assert not bundle.rejected, bundle.rejected
    assert bundle.plans
    return bundle


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")


def _row_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


class _ProbeHandler(BaseHTTPRequestHandler):
    db_path: Path

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        payload = json.dumps({"count": _row_count(self.db_path)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        return


def _serve(db_path: Path):
    handler = type("Handler", (_ProbeHandler,), {"db_path": db_path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_data_fixture_applies_before_request_and_tears_down(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ASTSIGHT_DB_URL", raising=False)
    case = _case()
    bundle = _bundle(case)
    db_path = tmp_path / "app.db"
    _make_db(db_path)
    generated = tmp_path / "generated"

    result = generate_pytest([case], generated, data_bundle=bundle)
    assert not result.errors
    assert (generated / "data_plan.json").is_file()
    conftest = (generated / "conftest.py").read_text(encoding="utf-8")
    assert "def data_state" in conftest
    test_file = next(generated.glob("test_*.py"))
    source = test_file.read_text(encoding="utf-8")
    assert "@pytest.mark.astsight_data('TC-0001', requires_data=True)" in source

    server, base_url = _serve(db_path)
    try:
        plan_path = str(generated / "data_plan.json")
        db_url = f"sqlite:///{db_path.as_posix()}"
        ok, output = collect_only(
            generated, base_url=base_url,
            data_plan_path=plan_path, database_url=db_url,
        )
        assert ok, output
        execution = run_pytest(
            generated, base_url=base_url,
            data_plan_path=plan_path, database_url=db_url,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert execution.records[0].outcome == "passed", execution.stdout + execution.stderr
    # fixture 在用例结束后按主键回收了本次造数。
    assert _row_count(db_path) == 0


def test_data_fixture_is_blocked_without_database(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ASTSIGHT_DB_URL", raising=False)
    case = _case()
    bundle = _bundle(case)
    generated = tmp_path / "generated"
    generate_pytest([case], generated, data_bundle=bundle)

    server, base_url = _serve(tmp_path / "app.db")
    try:
        execution = run_pytest(
            generated, base_url=base_url,
            data_plan_path=str(generated / "data_plan.json"),
            database_url=None,
        )
    finally:
        server.shutdown()
        server.server_close()

    record = execution.records[0]
    assert record.outcome == "blocked"
    assert record.message.startswith("data_unavailable")


def test_data_fixture_rejects_plan_dialect_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ASTSIGHT_DB_URL", raising=False)
    case = _case()
    bundle = _bundle(case)
    generated = tmp_path / "generated"
    generate_pytest([case], generated, data_bundle=bundle)

    server, base_url = _serve(tmp_path / "app.db")
    try:
        execution = run_pytest(
            generated, base_url=base_url,
            data_plan_path=str(generated / "data_plan.json"),
            database_url="postgresql://user:pass@127.0.0.1:1/appdb",
        )
    finally:
        server.shutdown()
        server.server_close()

    record = execution.records[0]
    assert record.outcome == "blocked"
    assert "data_dialect_mismatch" in record.message


def test_preconditions_without_plan_are_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ASTSIGHT_DB_URL", raising=False)
    monkeypatch.delenv("ASTSIGHT_DATA_PLAN_PATH", raising=False)
    case = _case()
    generated = tmp_path / "generated"
    generate_pytest([case], generated)

    server, base_url = _serve(tmp_path / "app.db")
    try:
        execution = run_pytest(generated, base_url=base_url)
    finally:
        server.shutdown()
        server.server_close()

    record = execution.records[0]
    assert record.outcome == "blocked"
    assert "no data plan" in record.message
