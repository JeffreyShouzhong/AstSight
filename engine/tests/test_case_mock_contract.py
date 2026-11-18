"""用例绑定的 Mock 分支与 A 侧响应契约一致性校验。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cases.context import build_context
from astsight.cases.draft import CaseDraft
from astsight.cases.validate import validate_draft
from contracts import (
    CaseMockRule,
    Evidence,
    MockBranch,
    MockBranchSource,
    MockDep,
    MockSemantic,
    MockRequestMatch,
    MockStub,
    MockStrategy,
    Transport,
)

DEP_ID = "http:svc:item-api GET /items"

API_DOC = """# 订单服务

## POST /orders

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| customer_id | string | 是 | 长度 3-40 |
"""

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "demo", "version": "1.0"},
    "paths": {
        "/orders": {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"customer_id": {"type": "string"}},
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "created"}},
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


def _with_contract(context: dict, *, types: list[dict], body: dict) -> tuple[dict, MockSemantic]:
    profile = next(p for p in context["profiles"] if p["endpoint"] == "POST /orders")
    profile["downstream_deps"] = [
        {
            "dep_id": DEP_ID,
            "method": "GET",
            "path_key": "/items",
            "service_name": "item-api",
            "response_types": [types[0].get("name", "Item")],
            "response_contract": {
                "status": "resolved",
                "types": types,
                "unresolved": [],
            },
        }
    ]
    semantic = MockSemantic(
        context_id=context["context_id"],
        generated_at="2026-11-13T00:00:00Z",
        deps=[
            MockDep(
                dep_id=DEP_ID,
                transport=Transport.HTTP,
                endpoint="GET /items",
                route_key="/items",
                strategy=MockStrategy.URL_OVERRIDE,
                branches=[
                    MockBranch(
                        branch="success",
                        semantics="下游正常返回",
                        stub=MockStub(status=200, body=body),
                        evidence=Evidence.INFERRED,
                        source=MockBranchSource(kind="contract", rule_keys=[]),
                    )
                ],
            )
        ],
    )
    return context, semantic


def _draft(context: dict) -> CaseDraft:
    return CaseDraft.model_validate(
        {
            "schema_version": 1,
            "context_id": context["context_id"],
            "generator": {"kind": "manual", "name": "test"},
            "scope_docs": ["docs/api.md"],
            "cases": [
                {
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
                    "asserts": [{"kind": "status_code", "op": "eq", "expect": 201}],
                    "assert_strength": "L1",
                    "mock_bindings": {DEP_ID: "success"},
                }
            ],
            "uncovered_requirements": [],
        }
    )


ITEM_TYPE = {
    "name": "Item",
    "fqn": "com.example.item.Item",
    "kind": "source",
    "origin": "src/main/java/com/example/item/Item.java",
    "container": None,
    "fields": [
        {"name": "id", "type": "Long"},
        {"name": "name", "type": "String"},
    ],
}


def _drift_warnings(result) -> list:
    return [item for item in result.warnings if item.code == "mock.body_contract_drift"]


def test_success_body_matching_contract_has_no_warning(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE], body={"id": 1, "name": "box"}
    )
    result = validate_draft(
        _draft(context), context, project=tmp_path, mock_semantic=semantic
    )
    assert result.errors == []
    assert _drift_warnings(result) == []


def test_success_body_missing_contract_field_warns(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE], body={"id": 1}
    )
    result = validate_draft(
        _draft(context), context, project=tmp_path, mock_semantic=semantic
    )
    assert result.errors == []
    warnings = _drift_warnings(result)
    assert len(warnings) == 1
    assert "name" in warnings[0].message
    assert "Item" in warnings[0].message


def test_list_stub_body_uses_first_element(tmp_path: Path) -> None:
    context = _context(tmp_path)
    item_type = dict(ITEM_TYPE, container="list")
    context, semantic = _with_contract(
        context, types=[item_type], body=[{"id": 1, "name": "box"}]
    )
    result = validate_draft(
        _draft(context), context, project=tmp_path, mock_semantic=semantic
    )
    assert _drift_warnings(result) == []


def test_request_rules_and_sequence_are_validated(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _draft(context)
    case = draft.cases[0].model_copy(
        update={
            "mock_rules": [
                CaseMockRule(
                    dep_id=DEP_ID,
                    branch="success",
                    match=MockRequestMatch(),
                ),
                CaseMockRule(
                    dep_id="http:svc:ghost GET /ghost",
                    branch="success",
                    match=MockRequestMatch(query={"id": "1"}),
                ),
            ],
            "mock_sequences": {
                DEP_ID: ["success"],
                "http:svc:other GET /other": ["success"],
            },
        }
    )
    result = validate_draft(
        draft.model_copy(update={"cases": [case]}),
        context,
        project=tmp_path,
    )

    codes = {item.code for item in result.errors}
    assert "mock.rule_match_empty" in codes
    assert "mock.rule_dep_missing_binding" in codes
    assert "mock.sequence_dep_missing_binding" in codes


def test_multiple_contract_types_are_not_checked(tmp_path: Path) -> None:
    context = _context(tmp_path)
    other = dict(ITEM_TYPE, name="Other", fqn="com.example.item.Other")
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE, other], body={}
    )
    result = validate_draft(
        _draft(context), context, project=tmp_path, mock_semantic=semantic
    )
    assert _drift_warnings(result) == []
