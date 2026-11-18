"""用例实体 → 插表 → 下游 Mock → 请求同源的真实链路。

SUT 是进程内 HTTP 服务：从 SQLite 读订单和明细，再调用商品下游。
用例声明的订单/商品实体同时用于插表、请求和下游 Mock 响应；
断言验证的是“用例自己造出来的那笔数据”。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from astsight.codegen import generate_pytest
from astsight.data import build_data_plan_bundle
from astsight.execute.runner import run_pytest
from astsight.mock import (
    CallSite,
    MockHttpServer,
    assign_route_keys,
    build_dep_id_plan,
)
from contracts import (
    Assertion,
    ColumnCatalog,
    DataCatalog,
    DataState,
    Evidence,
    ForeignKey,
    MockStub,
    Precondition,
    PreconditionKind,
    TableCatalog,
    TestCase as CaseModel,
)

SERVICE = "catalog-service"
DEP_ID = f"http:svc:{SERVICE} GET /products/{{}}"


def _catalog() -> DataCatalog:
    return DataCatalog(
        schema_version=1,
        catalog_id="cat-entity-e2e",
        project="demo",
        tables_order=["orders", "order_items"],
        tables=[
            TableCatalog(
                name="orders",
                primary_key=["id"],
                columns=[
                    ColumnCatalog(
                        name="id", type="TEXT", nullable=False,
                        primary_key=True, evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="status", type="TEXT", nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="product_id", type="TEXT", nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                ],
                evidence=Evidence.VERIFIED,
            ),
            TableCatalog(
                name="order_items",
                primary_key=["id"],
                columns=[
                    ColumnCatalog(
                        name="id", type="INTEGER", nullable=False,
                        primary_key=True, evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="order_id", type="TEXT", nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="sku", type="TEXT", nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                ],
                evidence=Evidence.VERIFIED,
            ),
        ],
        foreign_keys=[
            ForeignKey(
                child_table="order_items",
                child_columns=["order_id"],
                parent_table="orders",
                parent_columns=["id"],
                evidence=Evidence.VERIFIED,
            )
        ],
        states=[
            DataState(
                state_key="seed_order",
                table_values={},
                evidence=Evidence.VERIFIED,
            )
        ],
    )


def _case() -> CaseModel:
    return CaseModel(
        id="TC-9001",
        endpoint="GET /orders/{order_id}",
        category="business",
        method="GET",
        path="/orders/{order_id}",
        path_params={"order_id": {"$data": "order.id"}},
        source="test:entity-mock-e2e",
        data={
            "order": {"id": "O-1001", "product_id": "P-1001"},
            "product": {"id": "P-1001", "category": "phone", "name": "Mock 手机"},
        },
        preconditions=[
            Precondition(
                kind=PreconditionKind.DB_STATE,
                state_key="seed_order",
                table="orders",
                where={"id": {"$data": "order.id"}},
                evidence=Evidence.VERIFIED,
            )
        ],
        mock_bindings={DEP_ID: "success"},
        mock_replies={
            DEP_ID: {
                "id": {"$data": "product.id"},
                "category": {"$data": "product.category"},
                "name": {"$data": "product.name"},
            }
        },
        asserts=[
            Assertion(kind="status_code", op="eq", expect=200),
            Assertion(
                kind="body_field", target="order_id", op="eq", expect="O-1001"
            ),
            Assertion(kind="body_field", target="status", op="eq", expect="pending"),
            Assertion(
                kind="body_field",
                target="product_category",
                op="eq",
                expect="phone",
            ),
            Assertion(kind="body_field", target="item_count", op="eq", expect=1),
        ],
    )


def _fixtures() -> dict:
    case = _case()
    return {
        case.id: {
            "fixtures": {
                "orders": [
                    {
                        "id": "O-1001",
                        "status": "pending",
                        "product_id": "P-1001",
                    }
                ],
                "order_items": [
                    {"id": 1, "order_id": "O-1001", "sku": "SKU-1"}
                ],
            }
        }
    }


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE orders (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                product_id TEXT NOT NULL
            );
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY,
                order_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );
            """
        )


class _SutHandler(BaseHTTPRequestHandler):
    db_path: Path
    catalog_base: str

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        match = re.fullmatch(r"/orders/([^/]+)", self.path.partition("?")[0])
        if match is None:
            self.send_error(404)
            return
        order_id = match.group(1)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, status, product_id FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            items = conn.execute(
                "SELECT sku FROM order_items WHERE order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
        if row is None:
            self.send_error(404)
            return
        with urllib.request.urlopen(
            f"{self.catalog_base}/products/{row[2]}", timeout=5
        ) as response:
            product = json.loads(response.read().decode("utf-8"))
        payload = json.dumps(
            {
                "order_id": row[0],
                "status": row[1],
                "product_category": product["category"],
                "product_name": product["name"],
                "item_count": len(items),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        return


def _serve_sut(db_path: Path, catalog_base: str):
    handler = type(
        "Handler",
        (_SutHandler,),
        {"db_path": db_path, "catalog_base": catalog_base},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _mock_server(tmp_path: Path) -> tuple[MockHttpServer, str]:
    plan = build_dep_id_plan(
        [
            CallSite(
                "catalog-call",
                "GET",
                f"http://{SERVICE}/products/{{}}",
                service_name=SERVICE,
            )
        ]
    )
    dep = plan.deps[0]
    route_key = assign_route_keys(plan.deps)[dep.dep_id]
    server = MockHttpServer(
        sut_id="sut-entity",
        token="entity-token",
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={
            "success": MockStub(
                body={"id": "unknown", "category": "unknown", "name": "unknown"}
            )
        },
    )
    server.start()
    return server, dep.dep_id


def test_case_entity_seed_and_downstream_mock_are_same_source(
    tmp_path: Path, monkeypatch
) -> None:
    for name in (
        "ASTSIGHT_DB_URL",
        "ASTSIGHT_MOCK_ADMIN_URL",
        "ASTSIGHT_MOCK_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    case = _case()
    bundle = build_data_plan_bundle(
        cases=[case], catalog=_catalog(), payloads=_fixtures()
    )
    assert not bundle.rejected, bundle.rejected
    assert bundle.plans[0].fixtures["orders"][0]["id"] == "O-1001"

    db_path = tmp_path / "sut.db"
    _make_db(db_path)
    mock, dep_id = _mock_server(tmp_path)
    sut, base_url = _serve_sut(db_path, mock.endpoint)
    generated = tmp_path / "generated"
    try:
        result = generate_pytest([case], generated, data_bundle=bundle)
        assert not result.errors
        execution = run_pytest(
            generated,
            base_url=base_url,
            data_plan_path=str(generated / "data_plan.json"),
            database_url=f"sqlite:///{db_path.as_posix()}",
            mock_admin_url=mock.admin_endpoint,
            mock_token=mock.token,
            mock_namespace="sut-entity",
        )
    finally:
        sut.shutdown()
        sut.server_close()
        mock.stop()

    assert execution.records[0].outcome == "passed", (
        execution.stdout + execution.stderr
    )
    calls = mock.evidence.records
    assert len(calls) == 1
    assert calls[0].dep_id == dep_id
    assert calls[0].result == "ok"
    # 用例结束后按主键回收了插表数据，下游也只被 Mock 命中一次。
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 0
