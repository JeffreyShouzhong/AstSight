"""DBML → 数据目录：枚举、行内/顶层引用、唯一索引与过滤。"""
from __future__ import annotations

from astsight.data import build_catalog, parse_dbml, validate_catalog

DBML = """
// 客户与订单
Enum order_status {
  pending [note: '待支付']
  paid
}

Table customers {
  id integer [pk, increment]
  email varchar(255) [not null, unique, note: '登录邮箱']
  Note: '客户表'
}

Table orders as o {
  id integer [pk]
  customer_id integer [not null]
  status order_status [not null, default: 'pending']
  total decimal(10, 2) [default: 0]
  Note: '订单表'
  indexes {
    (customer_id, status) [unique]
    total [name: 'idx_total_unique']
  }
}

Table order_items {
  order_id integer [not null, ref: > o.id]
  sku varchar(64) [not null]
}

Ref: o.id < order_items.order_id
"""


def test_dbml_parses_tables_columns_enums_and_refs() -> None:
    parsed = parse_dbml(DBML)
    tables = parsed.table_index()
    assert list(tables) == ["customers", "orders", "order_items"]

    customers = tables["customers"]
    assert customers.comment == "客户表"
    assert customers.primary_key == ["id"]
    email = customers.column_index()["email"]
    assert email.type == "VARCHAR(255)"
    assert email.nullable is False
    assert email.unique is True
    assert email.comment == "登录邮箱"

    orders = tables["orders"]
    assert orders.comment == "订单表"
    status = orders.column_index()["status"]
    assert status.enum_values == ["pending", "paid"]
    assert status.type == "ENUM('pending', 'paid')"
    assert status.default == "pending"
    assert status.nullable is False
    total = orders.column_index()["total"]
    assert total.unique is False
    assert total.default == "0"

    fks = {
        (fk.child_table, tuple(fk.child_columns), fk.parent_table)
        for fk in parsed.foreign_keys
    }
    assert (
        "order_items",
        ("order_id",),
        "orders",
    ) in fks
    # 行内 ref 与顶层 Ref 指向同一条边，去重后只保留一条。
    assert len(
        [
            fk
            for fk in parsed.foreign_keys
            if fk.child_table == "order_items"
        ]
    ) == 1
    assert "dbml_unique_composite_ignored:orders:customer_id,status" in parsed.warnings


def test_dbml_catalog_is_buildable_and_valid() -> None:
    parsed = parse_dbml(DBML)
    catalog = build_catalog(project="demo", ddl=parsed, dialect="sqlite")
    assert validate_catalog(catalog) == []
    assert catalog.tables_order == ["customers", "orders", "order_items"]
    order = catalog.table_index()["orders"]
    assert order.primary_key == ["id"]
    status = order.column_index()["status"]
    assert status.semantic_type.value == "enum"


def test_dbml_unknown_ref_and_block_ref_become_warnings() -> None:
    parsed = parse_dbml(
        """
        Table a {
          id integer [pk]
          b_id integer [ref: > missing.id]
        }
        Ref: a.id > nope.id
        """
    )
    assert any(
        warning.startswith("dbml_ref_unknown_table")
        for warning in parsed.warnings
    )
    assert parsed.tables[0].foreign_keys == []
