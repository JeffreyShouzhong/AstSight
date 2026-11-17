"""data states 命令：列出业务态候选、未知 state 处理。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cli import main
from astsight.data import build_catalog, parse_ddl
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


def _rule(*constraints: RuleConstraint) -> Rule:
    return Rule(
        rule_key="RK-BR-01",
        rule_id="BR-01",
        text="创建订单",
        authority="B",
        source=RuleSource(doc="docs/r.md", section="规则", quote="创建订单"),
        constraints=list(constraints),
    )


def _catalog(rule: Rule):
    return build_catalog(
        project="demo",
        ddl=parse_ddl(DDL),
        ruleset=RuleSet(ruleset_id="rules-states", project="demo", rules=[rule]),
        rule_states=True,
    )


def test_data_states_cli_lists_candidates(tmp_path: Path, capsys) -> None:
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
    path = tmp_path / "catalog.json"
    path.write_text(catalog.model_dump_json(), encoding="utf-8")
    code = main(
        [
            "data",
            "states",
            "--project",
            str(tmp_path),
            "--catalog",
            str(path),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["states"] == 1
    assert payload["summary"]["rule_states"] == 1
    state = payload["states"][0]
    assert state["state_key"].startswith("rule_BR_01_")
    assert state["tables"] == {"orders": 1, "order_items": 1}
    assert state["errors"] == []
    assert any(
        gap.get("kind") == "state_placeholder" for gap in state["gaps"]
    )


def test_data_states_cli_rejects_unknown_state(tmp_path: Path, capsys) -> None:
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
    path = tmp_path / "catalog.json"
    path.write_text(catalog.model_dump_json(), encoding="utf-8")
    code = main(
        [
            "data",
            "states",
            "--project",
            str(tmp_path),
            "--catalog",
            str(path),
            "--state",
            "missing",
        ]
    )
    assert code == 1
    assert "state 不存在" in capsys.readouterr().err
