"""C5b 造数计划：catalog 白名单、FK 闭包、拓扑顺序、teardown。"""
from __future__ import annotations

import pytest

from astsight.data.plan import PlanError, build_data_plan, build_data_plan_bundle
from contracts import (
    ColumnCatalog,
    DataCatalog,
    DataState,
    Evidence,
    ForeignKey,
    Precondition,
    PreconditionKind,
    TableCatalog,
    TestCase as CaseModel,
)


def _catalog() -> DataCatalog:
    return DataCatalog(
        schema_version=1,
        catalog_id="cat-orders",
        project="demo",
        tables_order=["orders", "order_items"],
        tables=[
            TableCatalog(
                name="orders",
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
                        name="status",
                        type="TEXT",
                        nullable=False,
                        enum_values=["pending", "paid"],
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
                        name="id",
                        type="INTEGER",
                        nullable=False,
                        primary_key=True,
                        evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="order_id",
                        type="INTEGER",
                        nullable=False,
                        evidence=Evidence.VERIFIED,
                    ),
                    ColumnCatalog(
                        name="sku",
                        type="TEXT",
                        nullable=False,
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
                state_key="order_with_item",
                table_values={
                    "orders": [{"id": 10, "status": "pending"}],
                    "order_items": [{"id": 100, "order_id": 10, "sku": "SKU-1"}],
                },
                target_state={"orders": {"status": "pending"}},
                evidence=Evidence.VERIFIED,
            ),
            DataState(
                state_key="orphan_item",
                table_values={
                    "order_items": [{"id": 101, "order_id": 99, "sku": "SKU-X"}]
                },
                evidence=Evidence.INFERRED,
            ),
        ],
    )


def _case(state_key: str = "order_with_item", case_id: str = "TC-0001") -> CaseModel:
    return CaseModel(
        id=case_id,
        endpoint="GET /orders/10",
        category="positive",
        method="GET",
        path="/orders/10",
        source="test",
        preconditions=[
            Precondition(
                kind=PreconditionKind.DB_STATE,
                state_key=state_key,
                evidence=Evidence.VERIFIED,
            )
        ],
    )


def test_plan_covers_preconditions_in_topological_order() -> None:
    plan, rejected, warnings = build_data_plan(
        case=_case(), catalog=_catalog(), run_scope="r1"
    )
    assert rejected is None, rejected
    assert warnings == []
    assert plan is not None
    assert plan.run_scope == "r1"
    assert plan.catalog_id == "cat-orders"
    assert plan.target_state == {"orders": {"status": "pending"}}
    assert [statement["table"] for statement in plan.statements] == [
        "orders",
        "order_items",
    ]
    # teardown 逆拓扑：先删子表再删父表，且按主键参数化。
    assert [statement["table"] for statement in plan.teardown] == [
        "order_items",
        "orders",
    ]
    assert "DELETE FROM" in plan.teardown[0]["sql"]
    assert plan.teardown[0]["params"] == [100]
    assert plan.fk_links == ["order_items.order_id -> orders.id"]
    assert plan.preconditions[0]["state_key"] == "order_with_item"
    # 模型不直接提供 SQL：语句由 render 从通过校验的 fixtures 生成。
    assert all(statement["params"] is not None for statement in plan.statements)


def test_unknown_state_is_rejected() -> None:
    plan, rejected, _ = build_data_plan(case=_case("missing_state"), catalog=_catalog())
    assert plan is None
    assert rejected is not None
    assert rejected.reason == "data_plan_invalid"
    assert "state_unknown:missing_state" in rejected.details


def _case_without_state() -> CaseModel:
    case = _case()
    case.preconditions = []
    return case


def test_unknown_table_or_column_is_rejected() -> None:
    plan, rejected, _ = build_data_plan(
        case=_case_without_state(),
        catalog=_catalog(),
        fixtures={"ghost": [{"id": 1}]},
    )
    assert plan is None
    assert rejected is not None
    assert "fixture_table_unknown:ghost" in rejected.details

    plan, rejected, _ = build_data_plan(
        case=_case_without_state(),
        catalog=_catalog(),
        fixtures={"orders": [{"id": 1, "bogus": 2}]},
    )
    assert plan is None
    assert rejected is not None
    assert "fixture_column_unknown:orders[0].bogus" in rejected.details


def test_type_enum_and_not_null_are_enforced() -> None:
    plan, rejected, _ = build_data_plan(
        case=_case_without_state(),
        catalog=_catalog(),
        fixtures={"orders": [{"id": "ten", "status": "unknown"}]},
    )
    assert plan is None
    assert rejected is not None
    assert "fixture_type:orders[0].id:INTEGER" in rejected.details
    assert "fixture_enum:orders[0].status" in rejected.details

    plan, rejected, _ = build_data_plan(
        case=_case_without_state(),
        catalog=_catalog(),
        fixtures={"orders": [{"id": 1, "status": None}]},
    )
    assert plan is None
    assert rejected is not None
    assert "fixture_not_null:orders[0].status" in rejected.details


def test_fk_closure_requires_parent_row() -> None:
    plan, rejected, _ = build_data_plan(case=_case("orphan_item"), catalog=_catalog())
    assert plan is None
    assert rejected is not None
    assert "fk_parent_missing:order_items->orders" in rejected.details


def test_target_state_unknown_table_is_rejected() -> None:
    plan, rejected, _ = build_data_plan(
        case=_case(),
        catalog=_catalog(),
        target_state={"ghost": {"x": 1}},
    )
    assert plan is None
    assert rejected is not None
    assert rejected.reason == "target_state_invalid"
    assert "target_table_unknown:ghost" in rejected.details


def test_case_without_data_needs_no_plan() -> None:
    case = _case()
    case.preconditions = []
    plan, rejected, warnings = build_data_plan(case=case, catalog=_catalog())
    assert plan is None
    assert rejected is None
    assert warnings == []


def test_bundle_records_plans_and_rejections() -> None:
    bundle = build_data_plan_bundle(
        cases=[_case("order_with_item"), _case("missing_state", "TC-0002")],
        catalog=_catalog(),
        run_scope="scope-1",
    )
    assert bundle.run_scope == "scope-1"
    assert [plan.case_id for plan in bundle.plans] == ["TC-0001"]
    assert [item.case_id for item in bundle.rejected] == ["TC-0002"]
    assert bundle.rejected[0].reason == "data_plan_invalid"

    with pytest.raises(PlanError):
        build_data_plan_bundle(
            cases=[_case()] * 1001,
            catalog=_catalog(),
        )
