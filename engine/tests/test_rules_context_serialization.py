"""A1 规则上下文必须可 JSON 序列化（OpenAPI 参数不能泄漏 ParamSpec 对象）。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.rules.context import build_rules_context


def test_openapi_params_are_serialized(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "r.md").write_text(
        "# 推荐\n\n## GET /recommend\n\n返回手机商品。\n", encoding="utf-8"
    )
    (tmp_path / "openapi.json").write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "rec", "version": "1.0"},
                "paths": {
                    "/recommend": {
                        "get": {
                            "parameters": [
                                {
                                    "name": "product_id",
                                    "in": "query",
                                    "required": True,
                                    "schema": {"type": "string"},
                                }
                            ],
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    context = build_rules_context(
        tmp_path,
        ["docs/r.md"],
        openapi=str(tmp_path / "openapi.json"),
    )
    json.dumps(context)
    params = context["operations"][0]["params"]
    assert params[0]["name"] == "product_id"
    assert params[0]["in"] == "query"
