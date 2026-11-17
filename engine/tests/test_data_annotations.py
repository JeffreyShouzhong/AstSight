"""C5a 确定性语义候选：DB 注释、DBML Enum note 与规则常量折叠。"""
from __future__ import annotations

from astsight.data import (
    apply_semantics,
    build_catalog,
    comment_value_pairs,
    parse_dbml,
    parse_ddl,
    semantic_hint,
)
from astsight.data.semantics import SemanticDraft
from contracts import Rule, RuleConstraint, RuleSet, RuleSource


def _rule(
    *constraints: RuleConstraint, rule_key: str = "RK-fold", text: str = "状态规则"
) -> Rule:
    return Rule(
        rule_key=rule_key,
        rule_id="BR-01",
        text=text,
        authority="B",
        source=RuleSource(doc="docs/r.md", section="规则", quote="状态"),
        constraints=list(constraints),
    )


def _catalog(ddl: str, ruleset: RuleSet | None = None):
    return build_catalog(
        project="demo", ddl=parse_ddl(ddl), ruleset=ruleset
    )


def test_comment_semantic_hint_and_value_pairs() -> None:
    assert semantic_hint("订单金额，单位：分").value == "amount"
    assert semantic_hint("可售库存").value == "quantity"
    assert semantic_hint("是否启用").value == "boolean"
    assert semantic_hint("备注信息") is None

    assert comment_value_pairs("0=待支付；1=已支付") == [
        (0, "待支付"),
        (1, "已支付"),
    ]
    assert comment_value_pairs("待支付(0), 已支付(1)") == [
        (0, "待支付"),
        (1, "已支付"),
    ]
    assert comment_value_pairs("PAID=已支付") == [("PAID", "已支付")]
    # 前导零是编码形态，不能吞成整数。
    assert comment_value_pairs("01=待支付") == [("01", "待支付")]
    # 普通说明不是取值映射。
    assert comment_value_pairs("状态: 订单状态") == []


def test_catalog_uses_comment_hint_and_value_pairs() -> None:
    catalog = _catalog(
        """
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY,
          status TEXT NOT NULL COMMENT '0=待支付；1=已支付',
          flag TEXT COMMENT '是否启用',
          note TEXT COMMENT '备注'
        );
        """
    )
    order = catalog.table_index()["orders"].column_index()
    assert order["status"].semantic_type.value == "status"
    assert [(item.value, item.meaning) for item in order["status"].value_semantics] == [
        (0, "待支付"),
        (1, "已支付"),
    ]
    assert order["flag"].semantic_type.value == "boolean"
    assert order["note"].semantic_type.value == "text"
    assert not [
        gap for gap in catalog.gaps if gap.get("kind") == "field_semantics_unmapped"
    ]


def test_comment_values_conflicting_with_enum_are_dropped() -> None:
    catalog = _catalog(
        """
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY,
          status TEXT CHECK (status IN ('pending', 'paid'))
                 COMMENT 'pending=待支付；lost=已丢失'
        );
        """
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert [item.value for item in status.value_semantics] == ["pending", "paid"]
    assert "comment_value_unknown:orders.status:lost" in catalog.warnings


def test_dbml_enum_notes_and_table_purpose() -> None:
    parsed = parse_dbml(
        """
        Enum order_status {
          pending [note: '待支付']
          paid [note: '已支付']
          canceled
        }

        Table orders {
          id integer [pk]
          status order_status [not null, note: '订单状态']
          Note: '订单主表'
        }
        """
    )
    column = parsed.tables[0].column_index()["status"]
    assert column.value_meanings == {"pending": "待支付", "paid": "已支付"}

    catalog = build_catalog(project="demo", ddl=parsed)
    table = catalog.table_index()["orders"]
    assert table.purpose == "订单主表"
    status = table.column_index()["status"]
    assert [(item.value, item.meaning) for item in status.value_semantics] == [
        ("pending", "待支付"),
        ("paid", "已支付"),
        ("canceled", "canceled"),
    ]


def test_rule_constants_fold_into_value_semantics() -> None:
    ruleset = RuleSet(
        ruleset_id="rules-fold",
        rules=[
            _rule(
                RuleConstraint(
                    field="partner.status",
                    operator="eq",
                    values=["pending"],
                    condition="注册成功",
                ),
                RuleConstraint(
                    field="audit_step",
                    operator="eq",
                    values=["1"],
                    condition="初始审核步骤",
                ),
                RuleConstraint(
                    field="status",
                    operator="eq",
                    values=["lost"],
                    condition="歧义字段",
                ),
                RuleConstraint(
                    field="name",
                    operator="between",
                    values=["2", "20"],
                    condition="长度",
                ),
            )
        ],
    )
    catalog = _catalog(
        """
        CREATE TABLE partner (
          id INTEGER PRIMARY KEY,
          status TEXT,
          audit_step INTEGER,
          name TEXT
        );
        CREATE TABLE audit_log (
          id INTEGER PRIMARY KEY,
          status TEXT
        );
        """,
        ruleset=ruleset,
    )
    partner = catalog.table_index()["partner"].column_index()
    status = partner["status"].value_semantics
    assert [(item.value, item.meaning) for item in status] == [
        ("pending", "注册成功")
    ]
    assert status[0].rule_keys == ["RK-fold"]
    step = partner["audit_step"].value_semantics
    assert [(item.value, item.meaning) for item in step] == [
        (1, "初始审核步骤")
    ]
    assert partner["name"].value_semantics == []
    assert "constant_fold_ambiguous:status:2" in catalog.warnings


def test_rule_constant_conflicting_with_enum_is_not_folded() -> None:
    ruleset = RuleSet(
        ruleset_id="rules-fold-enum",
        rules=[
            _rule(
                RuleConstraint(
                    field="status",
                    operator="eq",
                    values=["lost"],
                    condition="订单丢失",
                )
            )
        ],
    )
    catalog = _catalog(
        """
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY,
          status TEXT CHECK (status IN ('pending', 'paid'))
        );
        """,
        ruleset=ruleset,
    )
    status = catalog.table_index()["orders"].column_index()["status"]
    assert [item.value for item in status.value_semantics] == ["pending", "paid"]
    assert (
        "constant_fold_enum_mismatch:orders.status:lost" in catalog.warnings
    )


def test_apply_semantics_folds_rule_constants_idempotently() -> None:
    ruleset = RuleSet(
        ruleset_id="rules-fold-apply",
        rules=[
            _rule(
                RuleConstraint(
                    field="status",
                    operator="eq",
                    values=["pending"],
                    condition="注册成功",
                )
            )
        ],
    )
    catalog = _catalog(
        "CREATE TABLE partner (id INTEGER PRIMARY KEY, status TEXT);"
    )
    assert catalog.table_index()["partner"].column_index()["status"].value_semantics == []

    first = apply_semantics(catalog, SemanticDraft(), ruleset=ruleset)
    assert first.ok
    status = catalog.table_index()["partner"].column_index()["status"]
    assert [(item.value, item.rule_keys) for item in status.value_semantics] == [
        ("pending", ["RK-fold"])
    ]

    second = apply_semantics(catalog, SemanticDraft(), ruleset=ruleset)
    assert second.ok
    status = catalog.table_index()["partner"].column_index()["status"]
    assert len(status.value_semantics) == 1
    assert status.value_semantics[0].rule_keys == ["RK-fold"]
