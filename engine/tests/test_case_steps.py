"""多请求用例：步骤解析、响应引用与 pytest 生成。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cases.context import build_context
from astsight.cases.draft import CaseDraft
from astsight.cases.proposal import _render_review_md
from astsight.cases.validate import validate_draft
from astsight.codegen import generate_pytest

API_DOC = """# 订单服务

## POST /orders

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| customer_id | string | 是 | 长度 3-40 |
| quantity | integer | 是 | 1 <= quantity <= 99 |

## PATCH /orders/{order_id}

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| status | string | 是 | 枚举 pending / paid |
"""

SPEC = {
    "openapi": "3.0.0",
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
                                "required": ["customer_id", "quantity"],
                                "properties": {
                                    "customer_id": {"type": "string"},
                                    "quantity": {"type": "integer", "minimum": 1},
                                },
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
                                    "type": "object",
                                    "properties": {"order_id": {"type": "integer"}},
                                }
                            }
                        },
                    },
                    "422": {"description": "validation error"},
                },
            }
        },
        "/orders/{order_id}": {
            "patch": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["status"],
                                "properties": {
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "paid"],
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "order_id": {"type": "integer"},
                                        "status": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "422": {"description": "validation error"},
                },
            }
        },
    },
}


def _project(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text(API_DOC, encoding="utf-8")
    (tmp_path / "openapi.json").write_text(
        json.dumps(SPEC, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def _context(tmp_path: Path) -> dict:
    return build_context(_project(tmp_path), ["docs/api.md"])


def _case(steps: list[dict], **overrides) -> dict:
    base = {
        "case_key": "create_order_then_pay",
        "title": "创建订单后支付",
        "level": "business",
        "category": "positive",
        "priority": "P0",
        "endpoint": "POST /orders",
        "requirement_refs": [
            {
                "doc": "docs/api.md",
                "section": "订单服务 > POST /orders",
                "quote": "1 <= quantity <= 99",
            }
        ],
        "steps": steps,
        "assert_strength": "L3",
    }
    base.update(overrides)
    return base


def _create_step() -> dict:
    return {
        "name": "create",
        "endpoint": "POST /orders",
        "body": {"customer_id": "cust_1", "quantity": 1},
        "asserts": [
            {"kind": "status_code", "op": "eq", "expect": 201},
            {"kind": "body_field", "target": "order_id", "op": "exists"},
        ],
    }


def _pay_step() -> dict:
    return {
        "name": "pay",
        "endpoint": "PATCH /orders/{order_id}",
        "path_params": {"order_id": {"$ref": "create.order_id"}},
        "body": {"status": "paid"},
        "asserts": [
            {"kind": "status_code", "op": "eq", "expect": 200},
            {
                "kind": "body_field",
                "target": "order_id",
                "op": "eq",
                "expect": {"$ref": "create.order_id"},
            },
            {
                "kind": "body_field",
                "target": "status",
                "op": "eq",
                "expect": "paid",
            },
        ],
    }


def _draft(context: dict, cases: list[dict]) -> CaseDraft:
    return CaseDraft.model_validate(
        {
            "schema_version": 1,
            "context_id": context["context_id"],
            "generator": {"kind": "manual", "name": "test"},
            "scope_docs": ["docs/api.md"],
            "cases": cases,
            "uncovered_requirements": [],
        }
    )


def test_multi_step_case_resolves_endpoints_and_refs(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(context, [_case([_create_step(), _pay_step()])]),
        context,
        project=tmp_path,
    )
    assert result.errors == []
    case = result.resolved[0].case
    assert [step.name for step in case.steps] == ["create", "pay"]
    assert case.steps[0].method == "POST"
    assert case.steps[1].method == "PATCH"
    assert case.steps[1].path == "/orders/{order_id}"
    assert case.asserts == []


def test_step_ref_must_point_to_earlier_step(tmp_path: Path) -> None:
    context = _context(tmp_path)
    pay = _pay_step()
    pay["path_params"] = {"order_id": {"$ref": "later.order_id"}}
    result = validate_draft(
        _draft(context, [_case([_create_step(), pay])]),
        context,
        project=tmp_path,
    )
    assert any(error.code == "step.ref_unknown" for error in result.errors)


def test_step_request_cannot_be_mixed_with_top_level_request(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            [
                _case(
                    [_create_step(), _pay_step()],
                    body={"customer_id": "cust_1", "quantity": 1},
                    asserts=[
                        {"kind": "status_code", "op": "eq", "expect": 201}
                    ],
                )
            ],
        ),
        context,
        project=tmp_path,
    )
    assert any(error.code == "case.steps_mixed_request" for error in result.errors)


def test_step_endpoint_must_be_in_context(tmp_path: Path) -> None:
    context = _context(tmp_path)
    pay = _pay_step()
    pay["endpoint"] = "DELETE /orders/{order_id}"
    result = validate_draft(
        _draft(context, [_case([_create_step(), pay])]),
        context,
        project=tmp_path,
    )
    assert any(error.code == "step.endpoint_unknown" for error in result.errors)


def test_step_header_ref_is_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path)
    pay = _pay_step()
    pay["headers"] = {"X-Order": {"$ref": "create.order_id"}}
    result = validate_draft(
        _draft(context, [_case([_create_step(), pay])]),
        context,
        project=tmp_path,
    )
    assert any(
        error.code == "step.header_ref_unsupported" for error in result.errors
    )


def test_steps_require_at_least_two(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(context, [_case([_create_step()])]),
        context,
        project=tmp_path,
    )
    assert any(error.code == "case.steps_too_few" for error in result.errors)


def test_step_names_must_be_unique(tmp_path: Path) -> None:
    context = _context(tmp_path)
    first = _create_step()
    second = _create_step()
    second["body"] = {"customer_id": "cust_2", "quantity": 2}
    result = validate_draft(
        _draft(context, [_case([first, second])]),
        context,
        project=tmp_path,
    )
    assert any(error.code == "step.name_duplicate" for error in result.errors)


def test_each_step_requires_status_assertion(tmp_path: Path) -> None:
    context = _context(tmp_path)
    create = _create_step()
    create["asserts"] = [
        {"kind": "body_field", "target": "order_id", "op": "exists"}
    ]
    result = validate_draft(
        _draft(context, [_case([create, _pay_step()])]),
        context,
        project=tmp_path,
    )
    assert any(error.code == "assertion.status_missing" for error in result.errors)


def test_primary_endpoint_must_appear_in_steps(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            [
                _case(
                    [_create_step(), _create_step()],
                    endpoint="PATCH /orders/{order_id}",
                )
            ],
        ),
        context,
        project=tmp_path,
    )
    assert any(
        error.code == "case.steps_primary_missing" for error in result.errors
    )


def test_single_request_case_rejects_step_ref(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(
            context,
            [
                _case(
                    [],
                    body={
                        "customer_id": {"$ref": "create.customer_id"},
                        "quantity": 1,
                    },
                    asserts=[
                        {"kind": "status_code", "op": "eq", "expect": 201}
                    ],
                )
            ],
        ),
        context,
        project=tmp_path,
    )
    assert any(error.code == "step.ref_unknown" for error in result.errors)


def test_review_markdown_renders_each_step(tmp_path: Path) -> None:
    context = _context(tmp_path)
    draft = _draft(context, [_case([_create_step(), _pay_step()])])
    result = validate_draft(draft, context, project=tmp_path)
    assert result.errors == []
    candidate = result.resolved[0].case.model_dump(mode="json")
    proposal = {
        "proposal_id": "prop-test",
        "context_id": context["context_id"],
        "base_store_version": 0,
        "base_store_hash": "0" * 64,
        "draft_fingerprint": "0" * 64,
        "counts": {"new": 1},
        "items": [
            {
                "action": "new",
                "endpoint": "POST /orders",
                "case_key": "create_order_then_pay",
                "title": "创建订单后支付",
                "fingerprint": "0" * 64,
                "store_case_id": None,
                "current_version": None,
                "proposed_version": 1,
                "candidate": candidate,
                "existing": {},
                "warnings": [],
            }
        ],
    }
    markdown = _render_review_md(proposal, draft, [], None)
    assert "## 审核导航" in markdown
    assert "**多步骤**：1 条" in markdown
    assert "`#1` `create_order_then_pay`（2 步）" in markdown
    assert "| # | 风险 | action |" in markdown
    assert "#### 步骤 1 · `create` · `POST /orders`" in markdown
    assert "#### 步骤 2 · `pay` · `PATCH /orders/{order_id}`" in markdown
    assert '"$ref": "create.order_id"' in markdown
    assert "共享 client / Mock 会话 / 前置状态" in markdown


def test_generated_pytest_renders_sequence_and_refs(tmp_path: Path) -> None:
    context = _context(tmp_path)
    result = validate_draft(
        _draft(context, [_case([_create_step(), _pay_step()])]),
        context,
        project=tmp_path,
    )
    assert result.errors == []
    generated = tmp_path / "generated"
    codegen = generate_pytest([result.resolved[0].case], generated)
    assert codegen.errors == []
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in codegen.files
    )
    assert "resp_create = client.request(" in source
    assert "resp_pay = client.request(" in source
    assert "body_create = resp_create.json()" in source
    assert "step_tools.path(" in source
    assert "step_tools.refs(" in source
    assert "resp_pay.status_code == 200" in source
    assert "body_pay.get('order_id') == step_tools.refs(" in source
    compile(source.replace("import pytest", ""), "<generated>", "exec")
