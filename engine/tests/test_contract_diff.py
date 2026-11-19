"""OpenAPI 字段级契约 diff：参数、请求体、响应字段与状态码。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from astsight.changes.contract_diff import contract_diff
from astsight.changes.deleted import deleted_apis


def _git(project: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    completed = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert completed.returncode == 0, completed.stderr


def _spec(*, current: bool) -> dict:
    order_params = [
        {"name": "customer_id", "in": "query", "required": current,
         "schema": {"type": "string"}},
    ]
    if current:
        order_params.append(
            {"name": "locale", "in": "query", "required": False,
             "schema": {"type": "string"}}
        )
    body_props = {"sku": {"type": "string"}}
    body_props["note" if current else "qty"] = {
        "type": "string" if current else "integer"
    }
    body_required = ["sku"]
    if current:
        body_required.append("note")
    response_200 = {"id": {"type": "string"}, "status": {"type": "string"}}
    if current:
        response_200["total"] = {"type": "number"}
    responses = {"200": {"description": "ok", "content": {"application/json": {
        "schema": {"type": "object", "properties": response_200}}}}}
    if not current:
        responses["400"] = {"description": "bad", "content": {"application/json": {
            "schema": {"type": "object", "properties": {"error": {"type": "string"}}}}}}
    paths = {
        "/orders": {
            "post": {
                "parameters": order_params,
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": body_props,
                    "required": body_required,
                }}}},
                "responses": responses,
            }
        }
    }
    if current:
        paths["/new"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    else:
        paths["/old"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    return {"openapi": "3.1.0", "info": {"title": "x", "version": "1"},
            "paths": paths}


def test_contract_field_diff_reports_changes(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "test")
    spec = project / "openapi.json"
    spec.write_text(json.dumps(_spec(current=False)), encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    spec.write_text(json.dumps(_spec(current=True)), encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "change")

    state = contract_diff(
        project,
        base="HEAD~1",
        current_openapi=str(spec),
    )
    assert state["checked"] is True, state
    assert state["added_endpoints"] == ["GET /new"]
    assert state["removed_endpoints"] == ["GET /old"]
    change = next(item for item in state["changes"] if item["endpoint"] == "POST /orders")
    assert change["params"]["required_added"] == ["query.customer_id"]
    assert change["params"]["added"] == ["query.locale"]
    assert change["request_body"]["added"] == ["note"]
    assert change["request_body"]["removed"] == ["qty"]
    assert change["request_body"]["required_added"] == ["note"]
    assert change["responses"]["fields"]["200"]["added"] == ["total"]
    assert change["responses"]["removed_status_codes"] == [400]


def test_contract_diff_missing_openapi_is_explicit(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init")
    state = contract_diff(project, base="HEAD", current_openapi=None)
    assert state["checked"] is False
    assert any("openapi_missing" in item for item in state["warnings"])


def test_contract_changes_merge_into_changed_apis() -> None:
    from astsight.pipeline import _merge_contract_impacts

    impact = {"changed_apis": []}
    state = {
        "checked": True,
        "current_openapi": "openapi.json",
        "changes": [
            {
                "endpoint": "POST /orders",
                "method": "POST",
                "path": "/orders",
                "change_kinds": ["request_body"],
            }
        ],
    }
    _merge_contract_impacts(
        impact,
        state,
        routes=[
            {
                "method": "POST",
                "path": "/orders",
                "handler": "create_order",
                "file": "app.py",
                "source": "openapi",
            }
        ],
    )
    record = impact["changed_apis"][0]
    assert record["handler"] == "POST /orders"
    assert record["attribution"] == "contract_diff"
    assert record["change_kinds"] == ["request_body"]
