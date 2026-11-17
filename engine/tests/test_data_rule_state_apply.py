"""规则生成的状态 → data plan → SQLite 真实 apply/teardown 闭环。"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

from astsight.codegen import generate_pytest
from astsight.data import build_catalog, parse_ddl
from astsight.data.apply import apply_plan, teardown_plan
from astsight.data.plan import build_data_plan
from contracts import (
    Assertion,
    Precondition,
    Rule,
    RuleConstraint,
    RuleSet,
    RuleSource,
    TestCase as CaseModel,
)


def _rule(*constraints: RuleConstraint, text: str = "创建订单") -> Rule:
    return Rule(
        rule_key="RK-state-e2e",
        rule_id="BR-01",
        text=text,
        authority="B",
        source=RuleSource(doc="docs/r.md", section="规则", quote=text),
        constraints=list(constraints),
    )


def _catalog(ddl: str, rule: Rule):
    return build_catalog(
        project="demo",
        ddl=parse_ddl(ddl),
        ruleset=RuleSet(ruleset_id="rules-e2e", project="demo", rules=[rule]),
        rule_states=True,
    )


def _case(state_key: str) -> CaseModel:
    return CaseModel(
        id="TC-0001",
        endpoint="POST /orders",
        category="positive",
        method="POST",
        path="/orders",
        source="test",
        preconditions=[Precondition(state_key=state_key)],
    )


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _make_db(path: Path, ddl: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(ddl)


def test_cross_table_rule_state_reaches_database(tmp_path: Path) -> None:
    ddl = """
    CREATE TABLE orders (
      id INTEGER PRIMARY KEY,
      status TEXT NOT NULL,
      code TEXT NOT NULL
    );
    CREATE TABLE order_items (
      id INTEGER PRIMARY KEY,
      order_id INTEGER NOT NULL REFERENCES orders(id),
      qty INTEGER NOT NULL
    );
    CREATE TABLE audit_log (
      id INTEGER PRIMARY KEY,
      status TEXT NOT NULL
    );
    """
    catalog = _catalog(
        ddl,
        _rule(
            RuleConstraint(
                field="orders.code",
                operator="eq",
                values=["X-1"],
                condition="创建订单",
            ),
            RuleConstraint(
                field="order_items.qty",
                operator="eq",
                values=["2"],
                condition="创建订单",
            ),
            RuleConstraint(
                field="status",
                operator="eq",
                values=["pending"],
                condition="创建订单",
            ),
        ),
    )
    assert len(catalog.states) == 1
    state = catalog.states[0]
    plan, rejected, _ = build_data_plan(
        case=_case(state.state_key), catalog=catalog
    )
    assert rejected is None, rejected
    assert plan is not None
    assert plan.target_state == {}

    db_path = tmp_path / "app.db"
    _make_db(db_path, ddl)
    applied = apply_plan(db_url=_url(db_path), plan=plan, keep=True)
    assert applied.applied == 2

    with sqlite3.connect(db_path) as conn:
        order = conn.execute(
            "SELECT id, status, code FROM orders"
        ).fetchone()
        item = conn.execute(
            "SELECT id, order_id, qty FROM order_items"
        ).fetchone()
        assert order == (1, "pending", "X-1")
        assert item == (1, 1, 2)

    torn = teardown_plan(db_url=_url(db_path), plan=plan)
    assert torn.deleted == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 0
        )


def test_paired_transition_state_reaches_database_with_target(
    tmp_path: Path,
) -> None:
    ddl = """
    CREATE TABLE events (
      id INTEGER PRIMARY KEY,
      from_status TEXT,
      to_status TEXT NOT NULL
    );
    """
    catalog = _catalog(
        ddl,
        _rule(
            RuleConstraint(
                field="from_status",
                operator="eq",
                values=["pending"],
                condition="支付订单",
            ),
            RuleConstraint(
                field="to_status",
                operator="eq",
                values=["paid"],
                condition="支付订单",
            ),
            text="支付订单",
        ),
    )
    assert len(catalog.states) == 1
    state = catalog.states[0]
    plan, rejected, _ = build_data_plan(
        case=_case(state.state_key), catalog=catalog
    )
    assert rejected is None, rejected
    assert plan is not None
    assert plan.target_state == {"events": {"to_status": "paid"}}

    db_path = tmp_path / "events.db"
    _make_db(db_path, ddl)
    applied = apply_plan(db_url=_url(db_path), plan=plan, keep=True)
    assert applied.applied == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, from_status, to_status FROM events"
        ).fetchone()
        assert row == (1, "pending", "paid")

    torn = teardown_plan(db_url=_url(db_path), plan=plan)
    assert torn.deleted == 1


def _load_conftest(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "astsight_generated_conftest", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transition_state_generates_post_assertion() -> None:
    ddl = "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL);"
    catalog = _catalog(
        ddl,
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="transition",
                values=["pending", "paid"],
                condition="支付订单",
            ),
            text="支付订单",
        ),
    )
    state = catalog.states[0]
    plan, rejected, warnings = build_data_plan(
        case=_case(state.state_key), catalog=catalog
    )
    assert rejected is None, rejected
    assert plan is not None
    assert plan.post_assertions == [
        {
            "kind": "db_row",
            "table": "orders",
            "column": "status",
            "target": "orders.status",
            "op": "eq",
            "expect": "paid",
            "where": {"id": 1},
            "wait_ms": 0,
            "source": "transition_target",
        }
    ]
    assert warnings == []


def test_negative_case_does_not_generate_post_assertion() -> None:
    ddl = "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL);"
    catalog = _catalog(
        ddl,
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="transition",
                values=["pending", "paid"],
                condition="支付订单",
            ),
            text="支付订单",
        ),
    )
    state = catalog.states[0]
    case = _case(state.state_key).model_copy(update={"category": "negative"})
    plan, rejected, _warnings = build_data_plan(case=case, catalog=catalog)
    assert rejected is None
    assert plan is not None
    assert plan.post_assertions == []


def test_paired_transition_target_is_not_asserted() -> None:
    ddl = """
    CREATE TABLE events (
      id INTEGER PRIMARY KEY,
      from_status TEXT,
      to_status TEXT NOT NULL
    );
    """
    catalog = _catalog(
        ddl,
        _rule(
            RuleConstraint(
                field="from_status",
                operator="eq",
                values=["pending"],
                condition="支付订单",
            ),
            RuleConstraint(
                field="to_status",
                operator="eq",
                values=["paid"],
                condition="支付订单",
            ),
            text="支付订单",
        ),
    )
    state = catalog.states[0]
    plan, rejected, _warnings = build_data_plan(
        case=_case(state.state_key), catalog=catalog
    )
    assert rejected is None
    assert plan is not None
    assert plan.target_state == {"events": {"to_status": "paid"}}
    assert plan.post_assertions == []


def test_generated_post_assertion_runs_against_sqlite(tmp_path: Path) -> None:
    ddl = "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL);"
    case = CaseModel(
        id="TC-0001",
        endpoint="POST /orders",
        category="positive",
        method="POST",
        path="/orders",
        source="test",
        asserts=[
            Assertion(kind="status_code", op="eq", expect=200)
        ],
    )
    generated = generate_pytest([case], tmp_path / "generated")
    assert generated.errors == []
    module = _load_conftest(tmp_path / "generated" / "conftest.py")
    db_path = tmp_path / "post.db"
    _make_db(db_path, ddl)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (id, status) VALUES (1, 'paid')"
        )
    module.DB_URL = _url(db_path)
    plan = {
        "post_assertions": [
            {
                "table": "orders",
                "column": "status",
                "op": "eq",
                "expect": "paid",
                "where": {"id": 1},
                "wait_ms": 0,
            }
        ]
    }
    module._run_post_assertions(plan)
    plan["post_assertions"][0]["expect"] = "pending"
    import pytest

    with pytest.raises(AssertionError):
        module._run_post_assertions(plan)
