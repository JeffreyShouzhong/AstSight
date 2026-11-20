"""OpenAPI 解析（C3 契约输入的确定性部分）。

只做"读契约、取约束"：不推断业务语义、不调用 LLM。
缺失 OpenAPI 时由上层标 `contract_incomplete`，不降级猜测。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

_PARAM_RE = re.compile(r"(\{[^}/]+\}|<[^>/]+>)")
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def normalize_path(path: str) -> str:
    """把 `/orders/{id}` 与 `/orders/<int:id>` 归一到 `/orders/{}`，仅用于匹配。"""
    normalized = _PARAM_RE.sub("{}", path or "/")
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized or "/"


def load_spec(source: str | Path | None) -> dict:
    """从本地文件或 http(s) URL 读取 OpenAPI。失败抛异常——契约是事实源，不静默。"""
    if not source:
        return {}
    text = str(source)
    if text.startswith(("http://", "https://")):
        response = httpx.get(text, timeout=20.0)
        response.raise_for_status()
        raw = response.text
    else:
        raw = Path(text).read_text(encoding="utf-8")

    if raw.lstrip().startswith("{"):
        return json.loads(raw)
    return yaml.safe_load(raw) or {}


@dataclass
class ParamSpec:
    name: str
    location: str                     # path / query / header / cookie / body
    type: str = "string"
    required: bool = False
    constraints: dict[str, Any] = field(default_factory=dict)
    example: Any = None
    schema: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "in": self.location,
            "type": self.type,
            "required": self.required,
            "constraints": self.constraints,
            "example": self.example,
            "schema": self.schema,
        }


@dataclass
class EndpointSpec:
    method: str
    path: str
    operation_id: str = ""
    summary: str = ""
    params: list[ParamSpec] = field(default_factory=list)
    body_required: list[str] = field(default_factory=list)
    body_schema: dict[str, Any] = field(default_factory=dict)
    success_codes: list[int] = field(default_factory=lambda: [200])
    response_fields: dict[int, list[str]] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"{self.method} {self.path}"

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "path": self.path,
            "operation_id": self.operation_id,
            "summary": self.summary,
            "params": [p.as_dict() for p in self.params],
            "body_required": list(self.body_required),
            "body_schema": self.body_schema,
            "success_codes": list(self.success_codes),
            "response_fields": {str(k): v for k, v in self.response_fields.items()},
        }


def _deref(schema: Any, root: dict, depth: int = 0) -> dict:
    """解析本地 `$ref`（#/components/schemas/X）。深度保护防自引用。"""
    if depth > 12 or not isinstance(schema, dict):
        return schema if isinstance(schema, dict) else {}
    ref = schema.get("$ref")
    if not ref or not ref.startswith("#/"):
        return schema
    current: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(current, dict) or part not in current:
            return {}
        current = current[part]
    return _deref(current, root, depth + 1)


def _type_of(schema: dict) -> str:
    if "type" in schema:
        return str(schema["type"])
    if "anyOf" in schema and schema["anyOf"]:
        first = schema["anyOf"][0]
        return str(first.get("type", "string")) if isinstance(first, dict) else "string"
    return "string"


def _constraints(schema: dict) -> dict:
    keys = (
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "pattern", "enum", "format",
        "multipleOf", "minItems", "maxItems",
    )
    return {k: schema[k] for k in keys if k in schema and schema[k] is not None}


def _first_example(schema: dict) -> Any:
    for key in ("example", "default"):
        if key in schema and schema[key] is not None:
            return schema[key]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    if isinstance(examples, dict) and examples:
        first = next(iter(examples.values()))
        if isinstance(first, dict):
            return first.get("value")
        return first
    return None


def sample_value(schema: dict, root: dict, seed: str = "S1", depth: int = 0) -> Any:
    """按 schema 生成确定性的合法样例值。

    只用于正向用例的输入构造；业务上是否合法由被测服务判定，
    本函数不臆造业务约束（测试数据构造是 C5 的职责）。
    """
    schema = _deref(schema, root)
    if depth > 6 or not schema:
        return "T" + seed

    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    example = _first_example(schema)
    if example is not None:
        return example

    kind = _type_of(schema)
    if kind == "integer":
        low = schema.get("minimum", schema.get("exclusiveMinimum"))
        value = int(low) if isinstance(low, (int, float)) else 1
        if schema.get("exclusiveMinimum") is not None:
            value = int(schema["exclusiveMinimum"]) + 1
        high = schema.get("maximum")
        if isinstance(high, (int, float)):
            value = min(value, int(high))
        return value
    if kind == "number":
        low = schema.get("minimum", 1.0)
        value = float(low)
        if schema.get("exclusiveMinimum") is not None:
            value = float(schema["exclusiveMinimum"]) + 0.01
        return round(value, 2)
    if kind == "boolean":
        return True
    if kind == "array":
        item = _deref(schema.get("items", {}), root)
        return [sample_value(item, root, seed, depth + 1)]
    if kind == "object":
        result: dict[str, Any] = {}
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            result[name] = sample_value(
                _deref(properties.get(name, {}), root), root, f"{seed}_{name}", depth + 1
            )
        return result

    fmt = schema.get("format", "")
    if fmt == "email":
        return f"t{seed.lower()}@example.com"
    if fmt == "uuid":
        return "00000000-0000-4000-8000-000000000001"
    if fmt == "date":
        return "2026-01-01"
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "uri":
        return "https://example.com/t" + seed
    max_length = schema.get("maxLength")
    value = "T" + seed
    if isinstance(max_length, int):
        value = value[: max(1, max_length)]
    return value


def _append_params(
    target: list[ParamSpec], raw_params: list[dict], root: dict
) -> None:
    for raw in raw_params or []:
        if not isinstance(raw, dict):
            continue
        schema = _deref(raw.get("schema", {}), root)
        target.append(
            ParamSpec(
                name=str(raw.get("name", "")),
                location=str(raw.get("in", "query")),
                type=_type_of(schema),
                required=bool(raw.get("required", raw.get("in") == "path")),
                constraints=_constraints(schema),
                example=raw.get("example") if raw.get("example") is not None else _first_example(schema),
                schema=schema,
            )
        )


def parse_operations(spec: dict) -> list[EndpointSpec]:
    """把 OpenAPI 展开为端点列表（path × method）。"""
    operations: list[EndpointSpec] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters") or []
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            params: list[ParamSpec] = []
            _append_params(params, shared_params, spec)
            _append_params(params, operation.get("parameters") or [], spec)

            body_schema: dict[str, Any] = {}
            body_required: list[str] = []
            request_body = operation.get("requestBody") or {}
            content = (request_body.get("content") or {})
            json_body = content.get("application/json") or {}
            if json_body:
                body_schema = _deref(json_body.get("schema", {}), spec)
                body_required = list(body_schema.get("required") or [])

            responses = operation.get("responses") or {}
            success_codes: list[int] = []
            response_fields: dict[int, list[str]] = {}
            for code, response in responses.items():
                try:
                    numeric = int(str(code))
                except ValueError:
                    continue
                if 200 <= numeric < 300:
                    success_codes.append(numeric)
                # 错误响应体同样是契约事实：断言 error_code/message 时不能因为
                # 只收集 2xx 就把已声明的字段判成“未声明”。
                schema = {}
                content_obj = (response or {}).get("content") or {}
                json_resp = content_obj.get("application/json") or {}
                if json_resp:
                    schema = _deref(json_resp.get("schema", {}), spec)
                fields = list((schema.get("properties") or {}).keys())
                if fields:
                    response_fields[numeric] = fields

            operations.append(
                EndpointSpec(
                    method=method.upper(),
                    path=path,
                    operation_id=str(operation.get("operationId") or ""),
                    summary=str(operation.get("summary") or ""),
                    params=params,
                    body_required=body_required,
                    body_schema=body_schema,
                    success_codes=sorted(set(success_codes)) or [200],
                    response_fields=response_fields,
                )
            )
    return operations
