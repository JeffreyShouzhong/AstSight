"""OpenAPI 字段级契约 diff：base 与当前契约按端点比较。

P0 覆盖 HTTP OpenAPI：参数新增/删除/必填/类型，请求体字段与必填，
响应状态码与响应字段。只报告契约事实，不替模型猜业务影响。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from ..inventory import discover_openapi
from ..profile.openapi import EndpointSpec, load_spec, normalize_path, parse_operations


def contract_diff(
    project: str | Path,
    *,
    base: str,
    current_openapi: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    root = Path(project).resolve()
    state: dict[str, Any] = {
        "checked": False,
        "base": base,
        "changes": [],
        "added_endpoints": [],
        "removed_endpoints": [],
        "warnings": [],
    }
    if not base:
        state["warnings"].append("contract_diff_base_missing")
        return state
    current_source = discover_openapi(root, current_openapi, base_url)
    if not current_source:
        state["warnings"].append("contract_diff_openapi_missing")
        return state
    if _is_remote(current_source):
        state["warnings"].append("contract_diff_remote_openapi_unsupported")
        return state
    revision = _merge_base(root, base)
    if revision is None:
        state["warnings"].append("contract_diff_base_unresolved")
        return state
    state["revision"] = revision
    worktree = Path(tempfile.mkdtemp(prefix="astsight-contract-"))
    created = False
    try:
        added = _git(root, "worktree", "add", "--detach", str(worktree), revision)
        if added.returncode != 0:
            state["warnings"].append(
                "contract_diff_worktree_failed:"
                + (added.stderr or added.stdout or "")[:300]
            )
            return state
        created = True
        base_source = _base_source(root, worktree, current_source)
        if base_source is None:
            state["warnings"].append("contract_diff_base_openapi_missing")
            return state
        state["current_openapi"] = _portable(root, current_source)
        state["base_openapi"] = _portable(root, base_source)
        current_ops = _index(parse_operations(load_spec(current_source)))
        try:
            base_ops = _index(parse_operations(load_spec(base_source)))
        except Exception as exc:
            state["warnings"].append(
                f"contract_diff_base_parse_failed:{type(exc).__name__}:{exc}"[:500]
            )
            return state
        state["added_endpoints"] = sorted(set(current_ops) - set(base_ops))
        state["removed_endpoints"] = sorted(set(base_ops) - set(current_ops))
        changes = []
        for key in sorted(set(base_ops) & set(current_ops)):
            diff = _compare(base_ops[key], current_ops[key])
            if diff is not None:
                changes.append(diff)
        state["changes"] = changes
        state["checked"] = True
        return state
    except Exception as exc:
        state["warnings"].append(
            f"contract_diff_failed:{type(exc).__name__}:{exc}"[:500]
        )
        return state
    finally:
        if created:
            _git(root, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(worktree, ignore_errors=True)


def _index(operations: Iterable[EndpointSpec]) -> dict[str, EndpointSpec]:
    return {
        f"{op.method.upper()} {normalize_path(op.path)}": op
        for op in operations
    }


def _compare(base: EndpointSpec, current: EndpointSpec) -> dict[str, Any] | None:
    params = _params_diff(base, current)
    body = _body_diff(base, current)
    responses = _response_diff(base, current)
    if not params and not body and not responses:
        return None
    return {
        "endpoint": f"{current.method.upper()} {current.path}",
        "method": current.method.upper(),
        "path": current.path,
        "params": params,
        "request_body": body,
        "responses": responses,
        "change_kinds": _kinds(params, body, responses),
    }


def _params_diff(base: EndpointSpec, current: EndpointSpec) -> dict[str, Any]:
    left = {(item.location, item.name): item for item in base.params}
    right = {(item.location, item.name): item for item in current.params}
    added = sorted(f"{loc}.{name}" for loc, name in set(right) - set(left))
    removed = sorted(f"{loc}.{name}" for loc, name in set(left) - set(right))
    required_added = []
    required_removed = []
    type_changed = []
    for key in sorted(set(left) & set(right)):
        old = left[key]
        new = right[key]
        label = f"{key[0]}.{key[1]}"
        if not old.required and new.required:
            required_added.append(label)
        elif old.required and not new.required:
            required_removed.append(label)
        if old.type != new.type:
            type_changed.append(
                {"field": label, "before": old.type, "after": new.type}
            )
    out: dict[str, Any] = {}
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    if required_added:
        out["required_added"] = required_added
    if required_removed:
        out["required_removed"] = required_removed
    if type_changed:
        out["type_changed"] = type_changed
    return out


def _body_diff(base: EndpointSpec, current: EndpointSpec) -> dict[str, Any]:
    left = _properties(base.body_schema)
    right = _properties(current.body_schema)
    added = sorted(set(right) - set(left))
    removed = sorted(set(left) - set(right))
    required_added = sorted(set(current.body_required) - set(base.body_required))
    required_removed = sorted(set(base.body_required) - set(current.body_required))
    type_changed = []
    for name in sorted(set(left) & set(right)):
        old = _signature(left[name])
        new = _signature(right[name])
        if old != new:
            type_changed.append({"field": name, "before": old, "after": new})
    out: dict[str, Any] = {}
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    if required_added:
        out["required_added"] = required_added
    if required_removed:
        out["required_removed"] = required_removed
    if type_changed:
        out["type_changed"] = type_changed
    return out


def _response_diff(base: EndpointSpec, current: EndpointSpec) -> dict[str, Any]:
    left = base.response_fields
    right = current.response_fields
    added_codes = sorted(set(right) - set(left))
    removed_codes = sorted(set(left) - set(right))
    fields: dict[str, Any] = {}
    for code in sorted(set(left) & set(right)):
        before = set(left[code])
        after = set(right[code])
        item: dict[str, list[str]] = {}
        if after - before:
            item["added"] = sorted(after - before)
        if before - after:
            item["removed"] = sorted(before - after)
        if item:
            fields[str(code)] = item
    out: dict[str, Any] = {}
    if added_codes:
        out["added_status_codes"] = added_codes
    if removed_codes:
        out["removed_status_codes"] = removed_codes
    if fields:
        out["fields"] = fields
    success_added = sorted(set(current.success_codes) - set(base.success_codes))
    success_removed = sorted(set(base.success_codes) - set(current.success_codes))
    if success_added:
        out["success_added"] = success_added
    if success_removed:
        out["success_removed"] = success_removed
    return out


def _properties(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    return dict(properties) if isinstance(properties, dict) else {}


def _signature(schema: Any) -> str:
    if not isinstance(schema, dict):
        return json.dumps(schema, ensure_ascii=False, sort_keys=True)
    return json.dumps(
        {
            key: schema.get(key)
            for key in ("type", "format", "enum", "$ref", "items")
            if key in schema
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _kinds(params: dict, body: dict, responses: dict) -> list[str]:
    kinds: list[str] = []
    if params:
        kinds.append("param")
    if body:
        kinds.append("request_body")
    if responses:
        kinds.append("response")
    return kinds


def _base_source(root: Path, worktree: Path, current_source: str) -> str | None:
    path = Path(current_source)
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    candidate = worktree / relative
    if candidate.is_file():
        return str(candidate)
    # 当前显式文件在 base 不存在时，回退 base 自动发现。
    return discover_openapi(worktree, None, None)


def _is_remote(source: str) -> bool:
    return str(source).startswith(("http://", "https://"))


def _portable(root: Path, source: str) -> str:
    path = Path(source)
    if not path.is_absolute():
        path = root / path
    try:
        return str(path.resolve().relative_to(root))
    except (OSError, ValueError):
        return str(source)


def _merge_base(project: Path, base: str) -> str | None:
    result = _git(project, "merge-base", base, "HEAD")
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = _git(project, "rev-parse", "--verify", base)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


__all__ = ["contract_diff"]
