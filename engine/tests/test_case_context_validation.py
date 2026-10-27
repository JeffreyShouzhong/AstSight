from __future__ import annotations

import json
from pathlib import Path

import pytest

from astsight.cases.context import build_context, load_context, prepare_context
from astsight.cases.draft import (
    MAX_BODY_DEPTH,
    CaseDraft,
    case_fingerprint,
    load_draft,
)
from astsight.cases.requirements import parse_sections, read_documents
from astsight.cases.validate import validate_draft

API_DOC = """# 订单服务

本文档描述订单接口。

## POST /orders

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| customer_id | string | 是 | 长度 3-40 |
| quantity | integer | 是 | 1 <= quantity <= 99 |

```python
# 这不是标题
```

## 版本记录

历史变更不算需求。

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
                    }
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
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["status"],
                                "properties": {
                                    "status": {"type": "string", "enum": ["pending", "paid"]}
                                },
                            }
                        }
                    }
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


def _project(tmp_path: Path, *, api_doc: str = API_DOC) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text(api_doc, encoding="utf-8")
    (tmp_path / "openapi.json").write_text(
        json.dumps(SPEC, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def _draft(context: dict, cases: list[dict] | None = None, **overrides) -> CaseDraft:
    base = {
        "schema_version": 1,
        "context_id": context["context_id"],
        "generator": {"kind": "manual", "name": "test"},
        "scope_docs": ["docs/api.md"],
        "cases": cases or [],
        "uncovered_requirements": [],
    }
    base.update(overrides)
    return CaseDraft.model_validate(base)


def _case(**overrides) -> dict:
    base = {
        "case_key": "create_order_quantity_zero",
        "title": "quantity 非法",
        "requirement_refs": [
            {
                "doc": "docs/api.md",
                "section": "订单服务 > POST /orders",
                "quote": "1 <= quantity <= 99",
            }
        ],
        "endpoint": "POST /orders",
        "category": "boundary",
        "priority": "P0",
        "path_params": {},
        "query": {},
        "headers": {},
        "body": {"customer_id": "cust_1", "quantity": 0},
        "asserts": [
            {"kind": "status_code", "op": "in", "expect": [400, 422]}
        ],
        "assert_strength": "L1",
    }
    base.update(overrides)
    return base


def test_parse_sections_excludes_metadata_and_ignores_fenced_headings() -> None:
    sections = parse_sections(API_DOC)
    paths = {section.section: section for section in sections}
    assert "订单服务 > POST /orders" in paths
    assert "订单服务 > PATCH /orders/{order_id}" in paths
    assert "订单服务 > 版本记录" in paths
    assert paths["订单服务 > 版本记录"].include is False
    assert all("这不是标题" not in path for path in paths)
    # 规范化后的正文 hash 与空白变化无关
    again = {
        section.section: section
        for section in parse_sections(API_DOC.replace("| 字段 |", "|  字段  |"))
    }
    assert (
        paths["订单服务 > POST /orders"].text_hash
        == again["订单服务 > POST /orders"].text_hash
    )


def test_build_context_is_deterministic_and_writes_package(tmp_path: Path) -> None:
    project = _project(tmp_path)
    first = build_context(project, ["docs/api.md"])
    second = build_context(project, ["docs/api.md"])
    assert first["context_id"] == second["context_id"]
    assert {p["endpoint"] for p in first["profiles"]} == {
        "POST /orders",
        "PATCH /orders/{order_id}",
    }
    by_endpoint = {p["endpoint"]: p for p in first["profiles"]}
    assert by_endpoint["POST /orders"]["error_codes"] == [422]
    assert any(
        item["section"] == "订单服务 > 版本记录"
        for item in first["excluded_sections"]
    )
    prepared = prepare_context(project, ["docs/api.md"])
    assert prepared.context_path.is_file()
    assert prepared.prompt_path.is_file()
    assert prepared.schema_path.is_file()
    assert prepared.readme_path.is_file()
    loaded = load_context(prepared.context_dir)
    assert loaded["context_id"] == first["context_id"]


def test_valid_draft_resolves_and_fingerprint_ignores_doc_version(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context = build_context(project, ["docs/api.md"])
    draft = _draft(context, [_case()])
    result = validate_draft(draft, context, project=project)
    assert not result.errors, [item.model_dump() for item in result.errors]
    assert len(result.resolved) == 1
    resolved = result.resolved[0]
    assert resolved.case.endpoint == "POST /orders"
    assert resolved.case.requirement_refs[0].version == context["doc_versions"]["docs/api.md"]

    changed = resolved.case.model_copy(deep=True)
    changed.requirement_refs[0].version = "different"
    assert case_fingerprint(changed, resolved.case_key) == resolved.fingerprint

    changed.title = "另一个标题"
    changed.source = "skill:other"
    assert case_fingerprint(changed, resolved.case_key) == resolved.fingerprint

    changed.body = {"customer_id": "cust_1", "quantity": -1}
    assert case_fingerprint(changed, resolved.case_key) != resolved.fingerprint


def test_validation_rejects_unknown_endpoint_field_section_quote_and_duplicate(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    context = build_context(project, ["docs/api.md"])
    cases = [
        _case(
            case_key="bad_endpoint",
            endpoint="POST /missing",
        ),
        _case(
            case_key="bad_field",
            body={"customer_id": "cust_1", "quantity": 0, "unknown": 1},
        ),
        _case(
            case_key="bad_section",
            requirement_refs=[
                {
                    "doc": "docs/api.md",
                    "section": "订单服务 > 不存在",
                    "quote": "1 <= quantity <= 99",
                }
            ],
        ),
        _case(
            case_key="bad_quote",
            requirement_refs=[
                {
                    "doc": "docs/api.md",
                    "section": "订单服务 > POST /orders",
                    "quote": "这句不在原文里",
                }
            ],
        ),
        _case(
            case_key="bad_assert",
            asserts=[{"kind": "body_field", "target": "x", "op": "exists"}],
        ),
        _case(case_key="bad_assert"),
        _case(case_key="bad_assert"),
    ]
    result = validate_draft(_draft(context, cases), context, project=project)
    codes = {item.code for item in result.errors}
    assert "endpoint.unknown" in codes
    assert "request.body_field_unknown" in codes
    assert "requirement.section_unknown" in codes
    assert "requirement.quote_not_found" in codes
    assert "assertion.status_missing" in codes
    assert "case.duplicate_key" in codes


def test_validation_reports_unaccounted_and_uncovered(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context = build_context(project, ["docs/api.md"])
    draft = _draft(
        context,
        [_case()],
        uncovered_requirements=[
            {
                "doc": "docs/api.md",
                "section": "订单服务 > PATCH /orders/{order_id}",
                "reason": "needs_c5_state_setup",
            }
        ],
    )
    result = validate_draft(draft, context, project=project)
    assert not result.errors
    assert result.unaccounted == []
    assert result.scope_docs == ["docs/api.md"]
    assert any(item.code == "requirement.unaccounted" for item in result.warnings) is False


def test_validation_rejects_stale_context(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context = build_context(project, ["docs/api.md"])
    draft = _draft(context, [_case()])
    (project / "docs" / "api.md").write_text(API_DOC + "\n新增一行\n", encoding="utf-8")
    result = validate_draft(draft, context, project=project)
    assert any(item.code == "context.stale" for item in result.errors)


def test_path_traversal_document_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_documents([str(outside)], project)


def test_draft_body_depth_limit(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context = build_context(project, ["docs/api.md"])
    nested: dict = {}
    cursor = nested
    for _ in range(MAX_BODY_DEPTH + 2):
        cursor["x"] = {}
        cursor = cursor["x"]
    draft_path = project / "draft.json"
    draft_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "context_id": context["context_id"],
                "cases": [_case(body=nested)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="嵌套过深"):
        load_draft(draft_path)


def test_assertion_grounding_warning(tmp_path: Path) -> None:
    project = _project(tmp_path)
    context = build_context(project, ["docs/api.md"])
    # 422 在 OpenAPI 中声明，所以不告警
    draft = _draft(context, [_case()])
    result = validate_draft(draft, context, project=project)
    assert not any(item.code == "assertion.not_grounded" for item in result.warnings)

    # 409 没有声明，quote 也没有；应 warning
    case = _case(
        asserts=[{"kind": "status_code", "op": "eq", "expect": 409}]
    )
    result = validate_draft(_draft(context, [case]), context, project=project)
    assert any(item.code == "assertion.not_grounded" for item in result.warnings)
