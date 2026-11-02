"""C1 契约规则（CR）：从 OpenAPI operation 确定性提取。

CR 不使用模型；同一份契约每次提取结果必须逐字节一致。
v1 只覆盖 HTTP/OpenAPI；其他 transport 在调用方标 unsupported，不在此猜测。
"""
from __future__ import annotations

import json
from typing import Any

from contracts import ContractRule, RuleAuthority, RuleSource, RuleSourceKind, RuleStatus, Transport

from .validate import contract_rule_key

_MAX_FIELDS_PER_OPERATION = 200
_MAX_DEPTH = 6


def _constraint_text(kind: str, **values: Any) -> str:
    parts = [f"type={kind}"] if kind else []
    for key in sorted(values):
        value = values[key]
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return "; ".join(parts)


def _flatten_fields(
    schema: dict[str, Any],
    prefix: str,
    depth: int,
    out: list[tuple[str, dict[str, Any], bool]],
) -> None:
    if not isinstance(schema, dict) or depth > _MAX_DEPTH or len(out) >= _MAX_FIELDS_PER_OPERATION:
        return
    kind = str(schema.get("type") or "")
    if kind == "object" or "properties" in schema:
        required = {str(name) for name in schema.get("required") or []}
        for name, child in (schema.get("properties") or {}).items():
            if not isinstance(child, dict):
                continue
            path = f"{prefix}.{name}" if prefix else str(name)
            out.append((path, child, str(name) in required))
            if len(out) >= _MAX_FIELDS_PER_OPERATION:
                return
            _flatten_fields(child, path, depth + 1, out)
    elif kind == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            _flatten_fields(items, f"{prefix}[]", depth + 1, out)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """规则上下文把 operation/param 序列化成 dict，内存里是对象；二者都要支持。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _operation_fields(operation: Any) -> list[tuple[str, str]]:
    """返回 (field, constraint) 列表，field 带 location/body 前缀。"""
    fields: list[tuple[str, str]] = []
    for param in _get(operation, "params", []) or []:
        name = str(_get(param, "name", "") or "")
        if not name:
            continue
        location = str(_get(param, "location", "query") or "query")
        constraints = dict(_get(param, "constraints", {}) or {})
        if bool(_get(param, "required", False)):
            constraints["required"] = True
        kind = str(_get(param, "type", "string") or "string")
        text = _constraint_text(kind, **constraints)
        if text:
            fields.append((f"{location}.{name}", text))

    body = _get(operation, "body_schema", {}) or {}
    flattened: list[tuple[str, dict[str, Any], bool]] = []
    _flatten_fields(body, "body", 0, flattened)
    for path, schema, required in flattened[:_MAX_FIELDS_PER_OPERATION]:
        constraints = {
            key: schema[key]
            for key in (
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
                "minLength",
                "maxLength",
                "pattern",
                "enum",
                "format",
                "multipleOf",
                "minItems",
                "maxItems",
            )
            if schema.get(key) is not None
        }
        if required:
            constraints["required"] = True
        kind = str(schema.get("type") or "")
        text = _constraint_text(kind, **constraints)
        if text:
            fields.append((path, text))
    return fields


def build_contract_rules(
    operations: list[Any],
    *,
    source: str = "openapi",
    source_version: str | None = None,
) -> list[ContractRule]:
    rules: list[ContractRule] = []
    seen: set[str] = set()
    for operation in operations:
        endpoint = str(_get(operation, "endpoint", "") or "")
        if not endpoint:
            method = str(_get(operation, "method", "") or "")
            path = str(_get(operation, "path", "") or "")
            endpoint = f"{method} {path}".strip()
        if not endpoint:
            continue
        transport = Transport.HTTP
        for field_name, constraint in _operation_fields(operation):
            key = contract_rule_key(transport.value, endpoint, field_name, constraint)
            if key in seen:
                continue
            seen.add(key)
            rules.append(
                ContractRule(
                    rule_key=key,
                    rule_id="",
                    transport=transport,
                    endpoint=endpoint,
                    field=field_name,
                    constraint=constraint,
                    source=RuleSource(
                        doc=source,
                        section=endpoint,
                        kind=RuleSourceKind.EXPLICIT,
                        version=source_version,
                        quote=f"{field_name}: {constraint}",
                    ),
                    authority=RuleAuthority.A,
                    status=RuleStatus.ACTIVE,
                )
            )
    rules.sort(key=lambda rule: (rule.endpoint, rule.field, rule.constraint))
    for index, rule in enumerate(rules, 1):
        rule.rule_id = f"CR-{index:02d}"
    return rules


__all__ = ["build_contract_rules"]
