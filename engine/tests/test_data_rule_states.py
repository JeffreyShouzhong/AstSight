"""C5a 跨表业务态候选：规则约束 → DataState 骨架。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cli import main
from astsight.data import build_catalog, parse_ddl
from astsight.rules.validate import rule_fingerprint, rule_key
from contracts import Rule, RuleConstraint, RuleSet, RuleSource

DDL = """
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL
);
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  sku TEXT NOT NULL,
  qty INTEGER NOT NULL
);
"""


def _rule(
    *constraints: RuleConstraint,
    rule_id: str = "BR-01",
    text: str = "创建订单",
) -> Rule:
    return Rule(
        rule_key=f"RK-{rule_id}",
        rule_id=rule_id,
        text=text,
        authority="B",
        source=RuleSource(doc="docs/r.md", section="规则", quote="创建订单"),
        constraints=list(constraints),
    )


def _ruleset(rule: Rule) -> RuleSet:
    return RuleSet(ruleset_id="rules-states", project="demo", rules=[rule])


def _catalog(rule: Rule) -> object:
    return build_catalog(
        project="demo",
        ddl=parse_ddl(DDL),
        ruleset=_ruleset(rule),
        rule_states=True,
    )


def test_cross_table_rule_generates_state_with_fk_closure() -> None:
    catalog = _catalog(
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="eq",
                values=["pending"],
                condition="创建订单",
            ),
            RuleConstraint(
                field="order_items.qty",
                operator="eq",
                values=["2"],
                condition="创建订单",
            ),
        )
    )
    assert len(catalog.states) == 1
    state = catalog.states[0]
    assert state.state_key.startswith("rule_BR_01_")
    assert state.rule_keys == ["RK-BR-01"]
    assert state.table_values == {
        "orders": [{"status": "pending", "id": 1}],
        "order_items": [
            {"qty": 2, "id": 1, "order_id": 1, "sku": "ast_sku"}
        ],
    }
    assert catalog.sources["rule_states"] == {
        "proposed": 1,
        "placeholders": 1,
    }
    assert any(
        gap.get("kind") == "state_placeholder"
        and gap.get("table") == "order_items"
        and gap.get("column") == "sku"
        for gap in catalog.gaps
    )


def test_in_constraints_expand_to_value_combinations() -> None:
    catalog = _catalog(
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="in",
                values=["pending", "paid"],
                condition="创建订单",
            ),
            RuleConstraint(
                field="order_items.qty",
                operator="in",
                values=["1", "2"],
                condition="创建订单",
            ),
        )
    )
    assert len(catalog.states) == 4
    combos = {
        (
            state.table_values["orders"][0]["status"],
            state.table_values["order_items"][0]["qty"],
        )
        for state in catalog.states
    }
    assert combos == {
        ("pending", 1),
        ("pending", 2),
        ("paid", 1),
        ("paid", 2),
    }
    assert len({state.state_key for state in catalog.states}) == 4
    assert not any(
        gap.get("kind") == "state_value_combinations_truncated"
        for gap in catalog.gaps
    )


def test_in_combinations_are_capped_and_reported() -> None:
    catalog = _catalog(
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="in",
                values=["a1", "a2", "a3", "a4", "a5"],
                condition="创建订单",
            ),
            RuleConstraint(
                field="order_items.qty",
                operator="in",
                values=["1", "2", "3", "4", "5"],
                condition="创建订单",
            ),
        )
    )
    assert len(catalog.states) == 20
    assert any(
        gap.get("kind") == "state_value_combinations_truncated"
        for gap in catalog.gaps
    )


def test_transition_constraint_generates_before_and_target_state() -> None:
    catalog = _catalog(
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="transition",
                values=["pending", "paid"],
                condition="支付订单",
            )
        )
    )
    assert len(catalog.states) == 1
    state = catalog.states[0]
    assert state.state_key.startswith("transition_BR_01_")
    assert state.table_values == {"orders": [{"id": 1, "status": "pending"}]}
    assert state.target_state == {"orders": {"status": "paid"}}


def test_transition_can_combine_with_eq_constraints_on_other_tables() -> None:
    catalog = _catalog(
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="transition",
                values=["pending", "paid"],
                condition="支付订单",
            ),
            RuleConstraint(
                field="order_items.qty",
                operator="eq",
                values=["2"],
                condition="支付订单",
            ),
        )
    )
    assert len(catalog.states) == 1
    state = catalog.states[0]
    assert state.table_values["orders"][0]["status"] == "pending"
    assert state.table_values["order_items"][0]["qty"] == 2
    assert state.target_state == {"orders": {"status": "paid"}}


def test_transition_enum_mismatch_is_rejected() -> None:
    ddl = """
    CREATE TABLE orders (
      id INTEGER PRIMARY KEY,
      status TEXT CHECK (status IN ('pending', 'paid')) NOT NULL
    );
    """
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(ddl),
        ruleset=_ruleset(
            _rule(
                RuleConstraint(
                    field="orders.status",
                    operator="transition",
                    values=["pending", "lost"],
                    condition="支付订单",
                )
            )
        ),
        rule_states=True,
    )
    assert catalog.states == []
    assert any(
        warning.startswith("rule_state_enum_mismatch:orders.status:")
        for warning in catalog.warnings
    )


def test_transition_target_type_is_validated() -> None:
    ddl = """
    CREATE TABLE items (
      id INTEGER PRIMARY KEY,
      qty INTEGER NOT NULL
    );
    """
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(ddl),
        ruleset=_ruleset(
            _rule(
                RuleConstraint(
                    field="items.qty",
                    operator="transition",
                    values=["1", "not-a-number"],
                    condition="调整数量",
                )
            )
        ),
        rule_states=True,
    )
    assert catalog.states == []
    assert any(
        warning.startswith("rule_state_invalid:transition_BR_01_")
        for warning in catalog.warnings
    )


def test_single_table_constraints_do_not_create_cross_table_state() -> None:
    catalog = _catalog(
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="eq",
                values=["pending"],
                condition="创建订单",
            )
        )
    )
    assert catalog.states == []


def test_ambiguous_field_blocks_state_generation() -> None:
    ddl = """
    CREATE TABLE a (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
    CREATE TABLE b (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
    """
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(ddl),
        ruleset=_ruleset(
            _rule(
                RuleConstraint(
                    field="status",
                    operator="eq",
                    values=["pending"],
                    condition="创建",
                )
            )
        ),
        rule_states=True,
    )
    assert catalog.states == []
    assert any(
        warning.startswith("rule_state_field_ambiguous:status:")
        for warning in catalog.warnings
    )


def test_enum_mismatch_blocks_state_generation() -> None:
    ddl = """
    CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
    CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT NOT NULL);
    """
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(ddl),
        ruleset=_ruleset(
            _rule(
                RuleConstraint(
                    field="orders.status",
                    operator="eq",
                    values=["pending"],
                    condition="创建",
                ),
                RuleConstraint(
                    field="items.sku",
                    operator="eq",
                    values=["S-1"],
                    condition="创建",
                ),
            )
        ),
        rule_states=True,
    )
    # 没有数据库枚举时不会触发 mismatch；这里只验证结构可生成。
    assert len(catalog.states) == 1

    enum_ddl = """
    CREATE TABLE orders (
      id INTEGER PRIMARY KEY,
      status TEXT CHECK (status IN ('paid')) NOT NULL
    );
    CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT NOT NULL);
    """
    catalog = build_catalog(
        project="demo",
        ddl=parse_ddl(enum_ddl),
        ruleset=_ruleset(
            _rule(
                RuleConstraint(
                    field="orders.status",
                    operator="eq",
                    values=["pending"],
                    condition="创建",
                ),
                RuleConstraint(
                    field="items.sku",
                    operator="eq",
                    values=["S-1"],
                    condition="创建",
                ),
            )
        ),
        rule_states=True,
    )
    assert catalog.states == []
    assert any(
        warning.startswith("rule_state_enum_mismatch:orders.status:")
        for warning in catalog.warnings
    )


def test_invalid_candidate_is_reported_not_silently_dropped() -> None:
    catalog = _catalog(
        _rule(
            RuleConstraint(
                field="orders.status",
                operator="eq",
                values=["pending"],
                condition="创建订单",
            ),
            RuleConstraint(
                field="order_items.qty",
                operator="eq",
                values=["not-a-number"],
                condition="创建订单",
            ),
        )
    )
    assert catalog.states == []
    assert any(
        warning.startswith("rule_state_invalid:")
        for warning in catalog.warnings
    )


def test_data_catalog_cli_rule_states(tmp_path: Path, capsys) -> None:
    (tmp_path / "schema.sql").write_text(DDL, encoding="utf-8")
    text = "创建订单时状态为 pending 且明细数量为 2"
    source = RuleSource(
        doc="docs/r.md",
        section="规则",
        quote="创建订单时状态为 pending 且明细数量为 2",
    )
    rule = Rule(
        rule_key=rule_key("docs/r.md", "规则", text),
        rule_id="BR-01",
        text=text,
        authority="B",
        source=source,
        constraints=[
            RuleConstraint(
                field="orders.status",
                operator="eq",
                values=["pending"],
                condition="创建订单",
            ),
            RuleConstraint(
                field="order_items.qty",
                operator="eq",
                values=["2"],
                condition="创建订单",
            ),
        ],
    )
    rule.fingerprint = rule_fingerprint(rule)
    ruleset = RuleSet(
        ruleset_id="rules-states", project="demo", rules=[rule]
    )
    (tmp_path / "ruleset.json").write_text(
        ruleset.model_dump_json(), encoding="utf-8"
    )

    code = main(
        [
            "data",
            "catalog",
            "--project",
            str(tmp_path),
            "--ddl",
            "schema.sql",
            "--ruleset",
            "ruleset.json",
            "--rule-states",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rule_states"] == 1
    catalog = json.loads(Path(payload["catalog"]).read_text(encoding="utf-8"))
    assert len(catalog["states"]) == 1
    assert catalog["states"][0]["state_key"].startswith("rule_BR_01_")
