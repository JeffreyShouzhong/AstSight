"""gRPC / Dubbo 契约 diff：从 base 与当前源码重建服务契约并比较。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..mock.grpc_descriptor import GrpcDescriptorIndex
from ..profile.dubbo import scan_dubbo_sources

_SCALAR_TYPES = {
    1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32",
    6: "fixed64", 7: "fixed32", 8: "bool", 9: "string", 10: "group",
    11: "message", 12: "bytes", 13: "uint32", 14: "enum",
    15: "sfixed32", 16: "sfixed64", 17: "sint32", 18: "sint64",
}


def grpc_diff(root: Path, worktree: Path, state: dict[str, Any]) -> None:
    current, current_warnings, current_checked = _grpc_snapshot(root)
    section: dict[str, Any] = {
        "checked": False,
        "added_methods": [],
        "removed_methods": [],
        "changes": [],
        "warnings": current_warnings,
    }
    state["grpc"] = section
    if not current_checked:
        return
    base, base_warnings, base_checked = _grpc_snapshot(worktree)
    section["warnings"].extend(base_warnings)
    if not base_checked:
        section["warnings"].append("contract_diff_grpc_base_missing")
        return
    section["checked"] = True
    section["added_methods"] = sorted(set(current) - set(base))
    section["removed_methods"] = sorted(set(base) - set(current))
    changes = []
    for path in sorted(set(base) & set(current)):
        old = base[path]
        new = current[path]
        kinds = []
        if old["request_type"] != new["request_type"]:
            kinds.append("request_type")
        if old["response_type"] != new["response_type"]:
            kinds.append("response_type")
        if (
            old["client_streaming"] != new["client_streaming"]
            or old["server_streaming"] != new["server_streaming"]
        ):
            kinds.append("streaming")
        request_diff = _fields_diff(old["request_fields"], new["request_fields"])
        response_diff = _fields_diff(
            old["response_fields"], new["response_fields"]
        )
        if request_diff:
            kinds.append("request_field")
        if response_diff:
            kinds.append("response_field")
        if not kinds:
            continue
        changes.append(
            {
                "transport": "grpc",
                "endpoint": path,
                "service": new["service"],
                "method": new["method"],
                "request_type": new["request_type"],
                "response_type": new["response_type"],
                "change_kinds": kinds,
                "request": request_diff,
                "response": response_diff,
            }
        )
    section["changes"] = changes


def _grpc_snapshot(
    root: Path,
) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    methods: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    found = False
    for path in _walk(root, {".proto", ".protoset"}):
        try:
            index = GrpcDescriptorIndex.load(
                path, include_dirs=[root, path.parent]
            )
        except Exception as exc:
            warnings.append(
                f"grpc_contract_parse_failed:{_relative(root, path)}:"
                f"{type(exc).__name__}:{exc}"[:300]
            )
            continue
        found = True
        for method in index.all_methods():
            methods[method.grpc_path] = {
                "service": method.service,
                "method": method.method,
                "request_type": method.request_type,
                "response_type": method.response_type,
                "client_streaming": method.client_streaming,
                "server_streaming": method.server_streaming,
                "request_fields": _message_fields(index.pool, method.request_type),
                "response_fields": _message_fields(index.pool, method.response_type),
            }
    return methods, warnings, found


def _message_fields(pool: Any, type_name: str) -> dict[str, dict[str, Any]]:
    try:
        message = pool.FindMessageTypeByName(type_name)
    except KeyError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for field in message.fields:
        if field.message_type is not None:
            type_value = field.message_type.full_name
        elif field.enum_type is not None:
            type_value = field.enum_type.full_name
        else:
            type_value = _SCALAR_TYPES.get(field.type, str(field.type))
        out[str(field.number)] = {
            "name": field.name,
            "type": type_value,
            "repeated": bool(field.is_repeated),
            "optional": bool(getattr(field, "proto3_optional", False)),
            "oneof": field.containing_oneof.name
            if field.containing_oneof is not None
            else "",
            "enum_values": [
                value.name for value in field.enum_type.values
            ]
            if field.enum_type is not None
            else [],
        }
    return out


def _fields_diff(
    base: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    added = [
        dict(current[number], number=int(number))
        for number in sorted(set(current) - set(base), key=int)
    ]
    removed = [
        dict(base[number], number=int(number))
        for number in sorted(set(base) - set(current), key=int)
    ]
    changed = []
    for number in sorted(set(base) & set(current), key=int):
        old = base[number]
        new = current[number]
        fields = [
            key
            for key in (
                "name", "type", "repeated", "optional", "oneof", "enum_values"
            )
            if old.get(key) != new.get(key)
        ]
        if fields:
            changed.append(
                {
                    "number": int(number),
                    "name": new["name"],
                    "fields": fields,
                    "before": {key: old.get(key) for key in fields},
                    "after": {key: new.get(key) for key in fields},
                }
            )
    out: dict[str, Any] = {}
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    if changed:
        out["changed"] = changed
    return out


def dubbo_diff(root: Path, worktree: Path, state: dict[str, Any]) -> None:
    current, current_warnings, current_checked = _dubbo_snapshot(root)
    section: dict[str, Any] = {
        "checked": False,
        "added_methods": [],
        "removed_methods": [],
        "changes": [],
        "warnings": current_warnings,
    }
    state["dubbo"] = section
    if not current_checked:
        return
    base, base_warnings, base_checked = _dubbo_snapshot(worktree)
    section["warnings"].extend(base_warnings)
    if not base_checked:
        section["warnings"].append("contract_diff_dubbo_base_missing")
        return
    section["checked"] = True
    section["added_methods"] = sorted(set(current) - set(base))
    section["removed_methods"] = sorted(set(base) - set(current))
    changes = []
    for endpoint in sorted(set(base) & set(current)):
        old = base[endpoint]
        new = current[endpoint]
        kinds = []
        details: dict[str, Any] = {}
        if old["return_type"] != new["return_type"]:
            kinds.append("return_type")
            details["return_type"] = {
                "before": old["return_type"],
                "after": new["return_type"],
            }
        if old["parameter_types"] != new["parameter_types"]:
            kinds.append("parameter_types")
            details["parameter_types"] = {
                "before": old["parameter_types"],
                "after": new["parameter_types"],
            }
        parameter_diff = _schema_list_diff(
            old["parameter_schemas"], new["parameter_schemas"]
        )
        if parameter_diff:
            kinds.append("parameter_schema")
            details["parameter_schema"] = parameter_diff
        result_diff = _schema_diff(old["result_schema"], new["result_schema"])
        if result_diff:
            kinds.append("result_schema")
            details["result_schema"] = result_diff
        if not kinds:
            continue
        changes.append(
            {
                "transport": "dubbo",
                "endpoint": endpoint,
                "service": new["service"],
                "method": new["method"],
                "group": new["group"],
                "version": new["version"],
                "change_kinds": kinds,
                **details,
            }
        )
    section["changes"] = changes


def _dubbo_snapshot(
    root: Path,
) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    files = [
        (path, "")
        for path in _walk(root, {".java", ".kt", ".kts", ".xml"})
    ]
    if not files:
        return {}, [], False
    scan = scan_dubbo_sources(
        files=files,
        root=root,
        read_text=lambda path: path.read_text(encoding="utf-8", errors="replace"),
    )
    providers = {}
    for provider in scan.providers:
        endpoint = str(provider.get("endpoint") or "")
        if not endpoint:
            continue
        providers[endpoint] = {
            "service": provider.get("service", ""),
            "method": provider.get("method", ""),
            "group": provider.get("group", ""),
            "version": provider.get("version", ""),
            "return_type": provider.get("return_type", ""),
            "parameter_types": list(provider.get("parameter_types") or []),
            "parameter_schemas": list(provider.get("parameter_schemas") or []),
            "result_schema": provider.get("result_schema"),
        }
    warnings = list(scan.warnings)
    for item in (scan.unsupported or [])[:10]:
        warnings.append(
            f"dubbo_contract_unsupported:{item.get('reason')}:"
            f"{item.get('file')}"
        )
    return providers, warnings, bool(providers)


def _schema_list_diff(before: list[Any], after: list[Any]) -> dict[str, Any]:
    changed = []
    for index in range(min(len(before), len(after))):
        diff = _schema_diff(before[index], after[index])
        if diff:
            changed.append({"index": index, **diff})
    out: dict[str, Any] = {}
    if len(after) > len(before):
        out["added"] = after[len(before):]
    if len(before) > len(after):
        out["removed"] = before[len(after):]
    if changed:
        out["changed"] = changed
    return out


def _schema_diff(before: Any, after: Any) -> dict[str, Any]:
    if before == after:
        return {}
    before_fields = _top_fields(before)
    after_fields = _top_fields(after)
    if before_fields is None or after_fields is None:
        return {"before": before, "after": after}
    out: dict[str, Any] = {}
    added = sorted(set(after_fields) - set(before_fields))
    removed = sorted(set(before_fields) - set(after_fields))
    changed = []
    for name in sorted(set(before_fields) & set(after_fields)):
        old = before_fields[name]
        new = after_fields[name]
        if old != new:
            changed.append({"field": name, "before": old, "after": new})
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    if changed:
        out["changed"] = changed
    return out or {"before": before, "after": after}


def _top_fields(schema: Any) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    if schema.get("kind") in {"dto", "reference", "object"} and isinstance(
        schema.get("fields"), list
    ):
        return {
            str(field.get("name")): {
                "type": field.get("type"),
                "required": field.get("required"),
                "declared_type": field.get("declared_type"),
            }
            for field in schema["fields"]
            if isinstance(field, dict) and field.get("name")
        }
    if schema.get("kind") == "collection":
        return _top_fields(schema.get("element"))
    return None


def _walk(root: Path, suffixes: set[str]) -> list[Path]:
    result: list[Path] = []
    skip = {
        ".git", ".astsight", ".venv", "venv", "node_modules", "target",
        "build", "dist", "__pycache__", ".idea", ".pytest_cache",
        "test", "tests", "testing",
    }
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in skip and not name.startswith(".")
        )
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() not in suffixes:
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    continue
                if resolved.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            result.append(resolved)
            if len(result) >= 2_000:
                return result
    return result


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


__all__ = ["dubbo_diff", "grpc_diff"]
