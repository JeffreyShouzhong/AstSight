"""用例绑定的 Mock 分支与 A 侧响应契约一致性校验。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.agent.case_design import (
    _prompt_input,
    generate_case_design,
    load_case_design_prompt,
)
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
    Rule,
    RuleAuthority,
    RuleSet,
    RuleSource,
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


def _with_case_data(draft: CaseDraft, **updates) -> CaseDraft:
    case = draft.cases[0].model_copy(update=updates)
    return draft.model_copy(update={"cases": [case]})


def test_case_data_and_mock_reply_share_one_entity(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE], body={"id": 1, "name": "box"}
    )
    draft = _with_case_data(
        _draft(context),
        data={"product": {"id": "P-1001", "category": "phone"}},
        body={"customer_id": {"$data": "product.id"}},
        mock_replies={
            DEP_ID: {
                "id": {"$data": "product.id"},
                "category": {"$data": "product.category"},
            }
        },
    )
    result = validate_draft(
        draft, context, project=tmp_path, mock_semantic=semantic
    )
    assert result.errors == []
    assert [item for item in result.warnings if item.code == "data.unused"] == []
    resolved = result.resolved[0].case
    assert resolved.data["product"]["id"] == "P-1001"
    assert resolved.mock_replies[DEP_ID]["category"] == {
        "$data": "product.category"
    }


def test_unknown_data_ref_is_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _with_case_data(
        _draft(context),
        data={"product": {"id": "P-1001"}},
        body={"customer_id": {"$data": "product.sku"}},
    )
    result = validate_draft(draft, context, project=tmp_path)
    codes = {item.code for item in result.errors}
    assert "data.ref_unknown" in codes


def test_unused_data_entity_warns(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _with_case_data(
        _draft(context),
        data={"product": {"id": "P-1001"}},
    )
    result = validate_draft(draft, context, project=tmp_path)
    assert result.errors == []
    warnings = [item for item in result.warnings if item.code == "data.unused"]
    assert len(warnings) == 1
    assert "product" in warnings[0].message


def test_mock_reply_requires_binding(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _with_case_data(
        _draft(context),
        mock_replies={"http:svc:ghost GET /ghost": {"id": 1}},
    )
    result = validate_draft(draft, context, project=tmp_path)
    codes = {item.code for item in result.errors}
    assert "mock.reply_dep_missing_binding" in codes


def test_mock_reply_rejects_grpc_downstream(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE], body={"id": 1, "name": "box"}
    )
    grpc_semantic = semantic.model_copy(
        update={
            "deps": [
                semantic.deps[0].model_copy(update={"transport": Transport.GRPC})
            ]
        }
    )
    draft = _with_case_data(
        _draft(context),
        mock_replies={DEP_ID: {"id": 1}},
    )
    result = validate_draft(
        draft, context, project=tmp_path, mock_semantic=grpc_semantic
    )
    codes = {item.code for item in result.errors}
    assert "mock.reply_transport_unsupported" in codes


class _ScriptedLLM:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)

    def complete_json(self, *, system: str, user: str):
        return self.payloads.pop(0)


def _ruleset() -> RuleSet:
    return RuleSet(
        ruleset_id="rules-a4-data",
        rules=[
            Rule(
                rule_key="RK-a4-data",
                rule_id="BR-01",
                text="创建订单时 customer_id 指向一个手机商品",
                authority=RuleAuthority.B,
                source=RuleSource(
                    doc="docs/api.md",
                    section="订单服务 > POST /orders",
                    quote="customer_id",
                ),
            )
        ],
    )


def _a4_payload(context: dict, *, binding: dict[str, str]) -> dict:
    payload = _draft(context).model_dump(mode="json")
    case = payload["cases"][0]
    case.update(
        {
            "rule_refs": [
                {"rule_key": "RK-a4-data", "rule_id": "BR-01"}
            ],
            "data": {
                "product": {"id": "P-1001", "name": "Mock 手机"}
            },
            "body": {"customer_id": {"$data": "product.id"}},
            "mock_bindings": binding,
            "mock_replies": {
                DEP_ID: {
                    "id": {"$data": "product.id"},
                    "name": {"$data": "product.name"},
                }
            },
        }
    )
    return payload


def test_a4_prompt_exposes_mock_branch_body_and_entity_fields(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE], body={"id": 1, "name": "box"}
    )
    prompt_input = _prompt_input(
        context,
        ruleset=_ruleset(),
        mode="full",
        changed_apis=None,
        mock_semantic=semantic,
        data_catalog=None,
    )
    branch = prompt_input["mock_semantics"][0]["branches"][0]
    assert branch["body"] == {"id": 1, "name": "box"}
    case_properties = prompt_input["schema"]["$defs"]["DraftCase"]["properties"]
    assert "data" in case_properties
    assert "mock_replies" in case_properties
    prompt = load_case_design_prompt()
    assert "mock_replies" in prompt
    assert "不要从环境里找真实" in prompt


def test_a4_generates_entity_and_mock_reply(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE], body={"id": 1, "name": "box"}
    )
    result = generate_case_design(
        context,
        ruleset=_ruleset(),
        llm=_ScriptedLLM([_a4_payload(context, binding={})]),
        project=tmp_path,
        mock_semantic=semantic,
    )
    case = result.draft.cases[0]
    assert case.data["product"]["id"] == "P-1001"
    assert case.mock_replies[DEP_ID]["name"] == {"$data": "product.name"}
    assert case.mock_bindings == {DEP_ID: "success"}
    assert any(
        warning.startswith("default_success_binding:")
        for warning in result.warnings
    )


def test_mock_reply_unknown_field_warns(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context, semantic = _with_contract(
        context, types=[ITEM_TYPE], body={"id": 1, "name": "box"}
    )
    draft = _with_case_data(
        _draft(context),
        mock_replies={DEP_ID: {"id": 1, "ghost": "invented"}},
    )
    result = validate_draft(
        draft, context, project=tmp_path, mock_semantic=semantic
    )
    warnings = [
        item for item in result.warnings
        if item.code == "mock.reply_field_unknown"
    ]
    assert len(warnings) == 1
    assert "ghost" in warnings[0].message
