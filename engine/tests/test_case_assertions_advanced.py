"""高级断言：body_schema、db_row、header 与强度推导。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import threading
from pathlib import Path
from types import ModuleType

import pytest

from astsight.cases.context import build_context
from astsight.cases.draft import CaseDraft, UncoveredRequirement
from astsight.cases.strength import derive_assert_strength
from astsight.cases.templates import generate_cases
from astsight.cases.validate import validate_draft
from astsight.codegen import generate_pytest
from astsight.data.catalog import build_catalog
from astsight.data.ddl import parse_ddl
from contracts import (
    AssertStrength,
    Assertion,
    Rule,
    RuleRelation,
    RuleSet,
    RuleSource,
)

API_DOC = """# 订单服务

## POST /orders

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| customer_id | string | 是 | 长度 3-40 |
"""

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "demo", "version": "1.0"},
    "components": {
        "schemas": {
            "OrderResponse": {
                "type": "object",
                "required": ["order_id", "status"],
                "properties": {
                    "order_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "total": {"type": "integer"},
                    "items": {"type": "array"},
                    "echo": {"type": "string"},
                },
            }
        }
    },
    "paths": {
        "/orders": {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["customer_id"],
                                "properties": {"customer_id": {"type": "string"}},
                            }
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/OrderResponse"
                                }
                            }
                        },
                    },
                    "422": {"description": "validation error"},
                },
            }
        }
    },
}


def _context(tmp_path: Path) -> dict:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text(API_DOC, encoding="utf-8")
    (tmp_path / "openapi.json").write_text(
        json.dumps(SPEC, ensure_ascii=False), encoding="utf-8"
    )
    return build_context(tmp_path, ["docs/api.md"])


def _draft(context: dict, case: dict) -> CaseDraft:
    return CaseDraft.model_validate(
        {
            "schema_version": 1,
            "context_id": context["context_id"],
            "generator": {"kind": "manual", "name": "test"},
            "scope_docs": ["docs/api.md"],
            "cases": [case],
            "uncovered_requirements": [],
        }
    )


def _case(asserts: list[dict], **overrides) -> dict:
    base = {
        "case_key": "create_order",
        "title": "创建订单",
        "level": "business",
        "category": "positive",
        "priority": "P0",
        "endpoint": "POST /orders",
        "requirement_refs": [
            {
                "doc": "docs/api.md",
                "section": "订单服务 > POST /orders",
                "quote": "customer_id",
            }
        ],
        "body": {"customer_id": "cust_1"},
        "asserts": asserts,
        "assert_strength": "L1",
    }
    base.update(overrides)
    return base


def test_context_extracts_response_schema_and_content_type(tmp_path: Path) -> None:
    profile = _context(tmp_path)["profiles"][0]
    schema = profile["response_schemas"]["201"]
    assert schema["required"] == ["order_id", "status"]
    assert schema["properties"]["order_id"] == {"type": "integer"}
    assert profile["response_content_type"] == "application/json"


def test_body_schema_is_hydrated_and_generated(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _draft(
        context,
        _case(
            [
                {"kind": "status_code", "op": "eq", "expect": 201},
                {"kind": "body_schema", "target": "201", "op": "matches"},
            ]
        ),
    )
    result = validate_draft(draft, context, project=tmp_path)
    assert result.errors == []
    case = result.resolved[0].case
    schema_assert = next(a for a in case.asserts if a.kind == "body_schema")
    assert schema_assert.expect["required"] == ["order_id", "status"]

    generated = generate_pytest([case], tmp_path / "generated")
    assert generated.errors == []
    source = next(
        p for p in generated.files if p.name.startswith("test_post_")
    ).read_text(encoding="utf-8")
    assert "schema_tools.check(body, " in source
    assert "schema_tools" in source.split("def test_", 1)[1].split(")")[0]
    conftest = (tmp_path / "generated" / "conftest.py").read_text(encoding="utf-8")
    assert "class _SchemaTools" in conftest


def test_body_schema_unknown_status_is_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            _case(
                [
                    {"kind": "status_code", "op": "eq", "expect": 201},
                    {"kind": "body_schema", "target": "500", "op": "matches"},
                ]
            ),
        ),
        context,
        project=tmp_path,
    )
    assert any(error.code == "assertion.schema_unknown" for error in result.errors)


def _catalog():
    return build_catalog(
        project="demo",
        ddl=parse_ddl(
            "CREATE TABLE partners ("
            "id INTEGER PRIMARY KEY, mobile TEXT NOT NULL, status TEXT NOT NULL);"
        ),
    )


def test_db_row_requires_catalog_for_column_validation(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _draft(
        context,
        _case(
            [
                {"kind": "status_code", "op": "eq", "expect": 201},
                {
                    "kind": "db_row",
                    "target": "partners",
                    "op": "absent",
                    "where": {"mobile": "13800138000"},
                },
            ],
            category="negative",
        ),
    )
    without_catalog = validate_draft(draft, context, project=tmp_path)
    assert any(
        warning.code == "assertion.db_catalog_missing"
        for warning in without_catalog.warnings
    )
    with_catalog = validate_draft(
        draft, context, project=tmp_path, data_catalog=_catalog()
    )
    assert with_catalog.errors == []

    unknown = _draft(
        context,
        _case(
            [
                {"kind": "status_code", "op": "eq", "expect": 201},
                {"kind": "db_row", "target": "partners.unknown", "op": "absent"},
            ],
            case_key="unknown_column",
            category="negative",
        ),
    )
    rejected = validate_draft(
        unknown, context, project=tmp_path, data_catalog=_catalog()
    )
    assert any(
        error.code == "assertion.db_column_unknown" for error in rejected.errors
    )


def test_db_row_is_generated_with_fixture(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            _case(
                [
                    {"kind": "status_code", "op": "eq", "expect": 201},
                    {
                        "kind": "db_row",
                        "target": "partners",
                        "op": "absent",
                        "where": {"mobile": "13800138000"},
                    },
                ]
            ),
        ),
        context,
        project=tmp_path,
        data_catalog=_catalog(),
    )
    assert result.errors == []
    case = result.resolved[0].case
    generated = generate_pytest([case], tmp_path / "generated")
    assert generated.errors == []
    source = next(
        p for p in generated.files if p.name.startswith("test_post_")
    ).read_text(encoding="utf-8")
    assert "db_tools.check('partners', None, 'absent', None" in source
    assert "db_tools" in source.split("def test_", 1)[1].split(")")[0]


def test_assert_strength_is_derived_from_content(tmp_path: Path) -> None:
    assert (
        derive_assert_strength(
            [Assertion(kind="status_code", op="eq", expect=200)]
        )
        is AssertStrength.L1
    )
    assert (
        derive_assert_strength(
            [
                Assertion(kind="status_code", op="eq", expect=200),
                Assertion(kind="body_field", target="status", op="eq", expect="ok"),
            ]
        )
        is AssertStrength.L2
    )
    assert (
        derive_assert_strength(
            [
                Assertion(
                    kind="body_field",
                    target="order_id",
                    op="eq",
                    expect={"$ref": "create.order_id"},
                )
            ]
        )
        is AssertStrength.L3
    )
    assert (
        derive_assert_strength(
            [
                Assertion(
                    kind="body_field",
                    target="a",
                    op="eq",
                    expect={"$ref": "first.a"},
                ),
                Assertion(
                    kind="body_field",
                    target="b",
                    op="eq",
                    expect={"$ref": "second.b"},
                ),
            ],
            steps=2,
        )
        is AssertStrength.L4
    )


def test_same_response_relation_is_generated_and_evaluated(tmp_path: Path) -> None:
    context = _context(tmp_path)
    expression = {
        "$op": "sub",
        "args": [
            {"$field": "balance_before"},
            {"$field": "amount"},
        ],
    }
    result = validate_draft(
        _draft(
            context,
            _case(
                [
                    {"kind": "status_code", "op": "eq", "expect": 201},
                    {
                        "kind": "body_field",
                        "target": "order_id",
                        "op": "eq",
                        "expect": expression,
                    },
                ]
            ),
        ),
        context,
        project=tmp_path,
    )
    assert result.errors == []
    case = result.resolved[0].case
    assert case.assert_strength is AssertStrength.L3

    generated = generate_pytest([case], tmp_path / "generated")
    assert generated.errors == []
    source = next(
        path
        for path in generated.files
        if path.name.startswith("test_post_")
    ).read_text(encoding="utf-8")
    assert "step_tools.expr(" in source

    module = _load_conftest(tmp_path / "generated" / "conftest.py")
    actual = module._resolve_expression(
        expression,
        {},
        {"balance_before": 100, "amount": 25},
        None,
    )
    assert actual == 75
    with pytest.raises(AssertionError, match="missing response field"):
        module._resolve_expression(
            {"$field": "missing.value"},
            {},
            {"missing": {}},
            None,
        )


def test_relation_expression_supports_len_and_multiple_step_refs(
    tmp_path: Path,
) -> None:
    conftest_path = _generate_one(
        tmp_path,
        [
            {"kind": "status_code", "op": "eq", "expect": 201},
            {
                "kind": "body_field",
                "target": "order_id",
                "op": "eq",
                "expect": {
                    "$op": "len",
                    "args": [{"$field": "items"}],
                },
            },
        ],
    )
    module = _load_conftest(conftest_path)
    assert (
        module._resolve_expression(
            {"$op": "len", "args": [{"$field": "items"}]},
            {},
            {"items": [1, 2, 3]},
            None,
        )
        == 3
    )

    strength = derive_assert_strength(
        [
            Assertion(
                kind="body_field",
                target="total",
                op="eq",
                expect={
                    "$op": "add",
                    "args": [
                        {"$ref": "first.amount"},
                        {"$ref": "second.amount"},
                    ],
                },
            )
        ],
        steps=2,
    )
    assert strength is AssertStrength.L4


def test_expression_supports_aggregations_and_projection(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert {"avg", "min", "max", "count_distinct", "pluck"} <= set(
        context["assert_expression_ops"]
    )
    expression = {
        "$op": "avg",
        "args": [
            {"$op": "pluck", "args": [{"$field": "items"}, "price"]}
        ],
    }
    result = validate_draft(
        _draft(
            context,
            _case(
                [
                    {"kind": "status_code", "op": "eq", "expect": 201},
                    {
                        "kind": "body_field",
                        "target": "total",
                        "op": "eq",
                        "expect": expression,
                    },
                ]
            ),
        ),
        context,
        project=tmp_path,
    )
    assert result.errors == []
    generated = generate_pytest(
        [result.resolved[0].case], tmp_path / "generated"
    )
    assert generated.errors == []
    module = _load_conftest(tmp_path / "generated" / "conftest.py")

    body = {"total": 5, "items": [{"price": 4}, {"price": 6}]}
    assert module._resolve_expression(expression, {}, body, None) == 5
    assert (
        module._resolve_expression(
            {"$op": "min", "args": [{"$field": "values"}]},
            {},
            {"values": [3, 1, 2]},
            None,
        )
        == 1
    )
    assert (
        module._resolve_expression(
            {"$op": "max", "args": [{"$field": "values"}]},
            {},
            {"values": [3, 1, 2]},
            None,
        )
        == 3
    )
    # 去重按类型区分：True 与 1 不是同一个值，字典按内容归一。
    assert (
        module._resolve_expression(
            {"$op": "count_distinct", "args": [{"$field": "values"}]},
            {},
            {"values": [1, 1, "1", True, {"a": 1}, {"a": 1}]},
            None,
        )
        == 4
    )
    with pytest.raises(AssertionError, match="non-empty list"):
        module._resolve_expression(expression, {}, {"items": []}, None)
    with pytest.raises(AssertionError, match="missing field"):
        module._resolve_expression(
            expression, {}, {"items": [{"price": 4}, {}]}, None
        )
    with pytest.raises(AssertionError, match="requires a list"):
        module._resolve_expression(
            {"$op": "avg", "args": [{"$field": "total"}]},
            {},
            {"total": 5},
            None,
        )


def test_pluck_field_must_be_a_literal_name(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            _case(
                [
                    {"kind": "status_code", "op": "eq", "expect": 201},
                    {
                        "kind": "body_field",
                        "target": "total",
                        "op": "eq",
                        "expect": {
                            "$op": "sum",
                            "args": [
                                {
                                    "$op": "pluck",
                                    "args": [
                                        {"$field": "items"},
                                        {"$field": "price"},
                                    ],
                                }
                            ],
                        },
                    },
                ]
            ),
        ),
        context,
        project=tmp_path,
    )
    assert any(
        error.code == "expression.pluck_field_invalid"
        for error in result.errors
    )


def test_rule_relation_supports_aggregation_ops(tmp_path: Path) -> None:
    from astsight.rules.relations import validate_relation

    relation = RuleRelation(
        relation_key="RR-avg-price",
        assertion_kind="body_field",
        target="total",
        op="eq",
        expect={
            "$op": "avg",
            "args": [
                {
                    "$op": "pluck",
                    "args": [{"$response": "items"}, "price"],
                }
            ],
        },
        description="总额等于明细均价",
        evidence="customer_id",
    )
    assert validate_relation(relation) == []

    invalid = relation.model_copy(
        update={
            "expect": {
                "$op": "pluck",
                "args": [
                    {"$response": "items"},
                    {"$response": "price"},
                ],
            }
        }
    )
    assert any(
        code == "relation.pluck_field_invalid"
        for code, _message in validate_relation(invalid)
    )


def test_relation_expression_rejects_invalid_shape_and_request_use(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    invalid_assert = _draft(
        context,
        _case(
            [
                {"kind": "status_code", "op": "eq", "expect": 201},
                {
                    "kind": "body_field",
                    "target": "order_id",
                    "op": "eq",
                    "expect": {
                        "$op": "sub",
                        "args": [{"$field": "amount"}],
                    },
                },
            ]
        ),
    )
    result = validate_draft(invalid_assert, context, project=tmp_path)
    assert any(
        error.code == "expression.args_invalid" for error in result.errors
    )

    invalid_request = _draft(
        context,
        _case(
            [{"kind": "status_code", "op": "eq", "expect": 201}],
            case_key="field_ref_in_request",
            body={"customer_id": {"$field": "status"}},
        ),
    )
    result = validate_draft(invalid_request, context, project=tmp_path)
    assert any(
        error.code == "expression.field_not_allowed" for error in result.errors
    )


def test_rule_relations_are_materialized_from_keys(tmp_path: Path) -> None:
    context = _context(tmp_path)
    source = RuleSource(
        doc="docs/api.md",
        section="订单服务 > POST /orders",
        quote="customer_id",
    )
    rule = Rule(
        rule_key="RK-relation-test",
        rule_id="BR-01",
        text="总额与明细一致，回显字段与请求一致",
        authority="B",
        source=source,
        relations=[
            RuleRelation(
                relation_key="RR-total-items",
                assertion_kind="body_field",
                target="total",
                op="eq",
                expect={
                    "$op": "len",
                    "args": [{"$response": "items"}],
                },
                description="总额等于明细数量",
                evidence="customer_id",
            ),
            RuleRelation(
                relation_key="RR-echo-request",
                assertion_kind="body_field",
                target="echo",
                op="eq",
                expect={"$request": "customer_id"},
                description="echo 回显请求 customer_id",
                evidence="customer_id",
            ),
        ],
    )
    ruleset = RuleSet(
        ruleset_id="rules-relations",
        rules=[rule],
    )
    result = validate_draft(
        _draft(
            context,
            _case(
                [{"kind": "status_code", "op": "eq", "expect": 201}],
                rule_refs=[{"rule_key": rule.rule_key, "rule_id": rule.rule_id}],
                relation_refs=["RR-total-items", "RR-echo-request"],
            ),
        ),
        context,
        project=tmp_path,
        ruleset=ruleset,
    )
    assert result.errors == []
    case = result.resolved[0].case
    generated_relations = [
        assertion
        for assertion in case.asserts
        if assertion.relation_ref
    ]
    assert {item.relation_ref for item in generated_relations} == {
        "RR-total-items",
        "RR-echo-request",
    }
    assert case.assert_strength is AssertStrength.L3

    generated = generate_pytest([case], tmp_path / "generated")
    assert generated.errors == []
    module = _load_conftest(tmp_path / "generated" / "conftest.py")
    assert (
        module._resolve_expression(
            generated_relations[0].expect,
            {},
            {"total": 2, "items": [1, 2]},
            {"customer_id": "cust_1"},
        )
        == 2
    )
    assert (
        module._resolve_expression(
            generated_relations[1].expect,
            {},
            {"echo": "cust_1"},
            {"customer_id": "cust_1"},
        )
        == "cust_1"
    )


def test_rule_relation_must_be_covered_or_declared_uncovered(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    rule = Rule(
        rule_key="RK-relation-unaccounted",
        rule_id="BR-01",
        text="总额等于明细数量",
        authority="B",
        source=RuleSource(
            doc="docs/api.md",
            section="订单服务 > POST /orders",
            quote="customer_id",
        ),
        relations=[
            RuleRelation(
                relation_key="RR-unaccounted",
                target="total",
                op="eq",
                expect={
                    "$op": "len",
                    "args": [{"$response": "items"}],
                },
                description="总额等于明细数量",
                evidence="customer_id",
            )
        ],
    )
    ruleset = RuleSet(ruleset_id="rules-relations", rules=[rule])
    draft = _draft(
        context,
        _case(
            [{"kind": "status_code", "op": "eq", "expect": 201}],
            rule_refs=[{"rule_key": rule.rule_key, "rule_id": rule.rule_id}],
        ),
    )
    result = validate_draft(draft, context, project=tmp_path, ruleset=ruleset)
    assert any(error.code == "relation.unaccounted" for error in result.errors)

    draft.uncovered_requirements.append(
        UncoveredRequirement(
            doc="docs/api.md",
            section="订单服务 > POST /orders",
            reason="缺少 items 的可控数据",
            rule_keys=[rule.rule_key],
            relation_keys=["RR-unaccounted"],
        )
    )
    declared = validate_draft(draft, context, project=tmp_path, ruleset=ruleset)
    assert not any(
        error.code == "relation.unaccounted" for error in declared.errors
    )


def test_db_rule_relation_uses_response_and_request_refs(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    rule = Rule(
        rule_key="RK-db-relation",
        rule_id="BR-01",
        text="数据库状态与响应一致",
        authority="B",
        source=RuleSource(
            doc="docs/api.md",
            section="订单服务 > POST /orders",
            quote="customer_id",
        ),
        relations=[
            RuleRelation(
                relation_key="RR-db-status",
                assertion_kind="db_row",
                target="partners.status",
                op="eq",
                expect={"$response": "status"},
                where={"mobile": {"$request": "customer_id"}},
                wait_ms=500,
                description="落库状态与响应一致",
                evidence="customer_id",
            )
        ],
    )
    ruleset = RuleSet(ruleset_id="rules-db", rules=[rule])
    result = validate_draft(
        _draft(
            context,
            _case(
                [{"kind": "status_code", "op": "eq", "expect": 201}],
                rule_refs=[{"rule_key": rule.rule_key, "rule_id": rule.rule_id}],
                relation_refs=["RR-db-status"],
            ),
        ),
        context,
        project=tmp_path,
        ruleset=ruleset,
        data_catalog=_catalog(),
    )
    assert result.errors == []
    assertion = next(
        item for item in result.resolved[0].case.asserts
        if item.relation_ref == "RR-db-status"
    )
    assert assertion.kind == "db_row"
    assert assertion.wait_ms == 500
    assert assertion.expect == {"$field": "status"}
    assert assertion.where == {"mobile": {"$request": "customer_id"}}

    generated = generate_pytest(
        [result.resolved[0].case], tmp_path / "generated"
    )
    assert generated.errors == []
    source = next(
        path
        for path in generated.files
        if path.name.startswith("test_post_")
    ).read_text(encoding="utf-8")
    assert "db_tools.check('partners', 'status'" in source
    assert "wait_ms=500" in source


def test_declared_strength_mismatch_warns_and_derived_wins(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            _case(
                [
                    {"kind": "status_code", "op": "eq", "expect": 201},
                    {
                        "kind": "body_field",
                        "target": "order_id",
                        "op": "eq",
                        "expect": 1,
                    },
                ]
            ),
        ),
        context,
        project=tmp_path,
    )
    assert result.errors == []
    assert any(
        warning.code == "assert_strength.mismatch"
        for warning in result.warnings
    )
    assert result.resolved[0].case.assert_strength is AssertStrength.L2


def test_template_positive_adds_schema_and_header(tmp_path: Path) -> None:
    profile = _context(tmp_path)["profiles"][0]
    cases = generate_cases([profile])
    positive = next(case for case in cases if case.category == "positive")
    kinds = {assertion.kind for assertion in positive.asserts}
    assert {"status_code", "body_schema", "header"} <= kinds
    header = next(a for a in positive.asserts if a.kind == "header")
    assert header.target == "content-type"
    assert header.op == "contains"
    assert positive.assert_strength is AssertStrength.L2

    generated = generate_pytest([positive], tmp_path / "generated")
    assert generated.errors == []
    source = next(
        p for p in generated.files if p.name.startswith("test_post_")
    ).read_text(encoding="utf-8")
    assert "resp.headers.get('content-type'" in source


def _load_conftest(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "astsight_generated_conftest", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generate_one(tmp_path: Path, asserts: list[dict]) -> Path:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(context, _case(asserts)),
        context,
        project=tmp_path,
    )
    assert result.errors == []
    generated = generate_pytest([result.resolved[0].case], tmp_path / "generated")
    assert generated.errors == []
    source = next(p for p in generated.files if p.name.startswith("test_")).read_text(
        encoding="utf-8"
    )
    compile(source, "<generated>", "exec")
    return tmp_path / "generated" / "conftest.py"


def test_db_tools_runtime_against_sqlite(tmp_path: Path) -> None:
    conftest_path = _generate_one(
        tmp_path,
        [
            {"kind": "status_code", "op": "eq", "expect": 201},
            {"kind": "db_row", "target": "partners", "op": "exists"},
        ],
    )
    db_file = tmp_path / "state.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE partners (id INTEGER PRIMARY KEY, mobile TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO partners (mobile, status) VALUES ('13800138000', 'active')"
    )
    conn.commit()
    conn.close()

    module = _load_conftest(conftest_path)
    module.DB_URL = f"sqlite:///{db_file.as_posix()}"
    tools = module._DbTools()
    try:
        tools.check("partners", None, "exists", None, {"mobile": "13800138000"})
        tools.check(
            "partners", "status", "eq", "active", {"mobile": "13800138000"}
        )
        tools.check("partners", None, "count_eq", 1, {"status": "active"})
        tools.check("partners", "id", "gte", 1, {})
        with pytest.raises(AssertionError, match="expected no row"):
            tools.check("partners", None, "absent", None, {"mobile": "13800138000"})
        with pytest.raises(AssertionError, match="count 2, got 1"):
            tools.check("partners", None, "count_eq", 2, {})
        with pytest.raises(RuntimeError, match="db_assert_invalid"):
            tools.check("partners; DROP TABLE partners", None, "exists", None, {})
    finally:
        tools.close()
    assert tools._conn is None


def test_db_tools_skips_when_url_missing(tmp_path: Path) -> None:
    conftest_path = _generate_one(
        tmp_path,
        [
            {"kind": "status_code", "op": "eq", "expect": 201},
            {"kind": "db_row", "target": "partners", "op": "exists"},
        ],
    )
    module = _load_conftest(conftest_path)
    module.DB_URL = ""
    tools = module._DbTools()
    with pytest.raises(pytest.skip.Exception, match="data_unavailable"):
        tools.check("partners", None, "exists", None, {})
    assert tools._conn is None


def test_db_row_wait_ms_polls_until_row_is_visible(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _draft(
        context,
        _case(
            [
                {"kind": "status_code", "op": "eq", "expect": 201},
                {
                    "kind": "db_row",
                    "target": "partners",
                    "op": "exists",
                    "where": {"mobile": "13800138000"},
                    "wait_ms": 1000,
                },
            ]
        ),
    )
    result = validate_draft(draft, context, project=tmp_path)
    assert result.errors == []
    assertion = result.resolved[0].case.asserts[1]
    assert assertion.wait_ms == 1000

    generated = generate_pytest(
        [result.resolved[0].case], tmp_path / "generated"
    )
    assert generated.errors == []
    source = next(
        path
        for path in generated.files
        if path.name.startswith("test_post_")
    ).read_text(encoding="utf-8")
    assert "wait_ms=1000" in source

    db_file = tmp_path / "delayed.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE partners (id INTEGER PRIMARY KEY, mobile TEXT)")
    conn.commit()
    conn.close()

    def insert_row() -> None:
        delayed = sqlite3.connect(db_file)
        try:
            delayed.execute(
                "INSERT INTO partners (mobile) VALUES ('13800138000')"
            )
            delayed.commit()
        finally:
            delayed.close()

    worker = threading.Timer(0.12, insert_row)
    worker.start()
    module = _load_conftest(tmp_path / "generated" / "conftest.py")
    module.DB_URL = f"sqlite:///{db_file.as_posix()}"
    tools = module._DbTools()
    try:
        tools.check(
            "partners",
            None,
            "exists",
            None,
            {"mobile": "13800138000"},
            wait_ms=1000,
        )
    finally:
        worker.join(timeout=2)
        tools.close()


def test_wait_ms_is_rejected_outside_db_row(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            _case(
                [
                    {
                        "kind": "body_field",
                        "target": "status",
                        "op": "eq",
                        "expect": "ok",
                        "wait_ms": 100,
                    }
                ]
            ),
        ),
        context,
        project=tmp_path,
    )
    assert any(
        error.code == "assertion.wait_not_supported" for error in result.errors
    )


def test_schema_tools_runtime(tmp_path: Path) -> None:
    conftest_path = _generate_one(
        tmp_path,
        [
            {"kind": "status_code", "op": "eq", "expect": 201},
            {"kind": "body_schema", "target": "201", "op": "matches"},
        ],
    )
    module = _load_conftest(conftest_path)
    schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    tools = module._SchemaTools()
    tools.check({"id": 1}, schema)
    with pytest.raises(AssertionError, match="at id"):
        tools.check({"id": "not-int"}, schema)
    with pytest.raises(AssertionError, match="<root>"):
        tools.check({}, schema)
