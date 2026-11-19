"""C1 统一契约模型与分源确定性提取。

四种契约源先归一成 ``ContractModel``，CR 只从模型生成。解析器只提交可证明字段；
无法从静态源码证明的运行时语义进入 ``contract_gaps``，不推断补全。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..mock.grpc_descriptor import (
    GrpcDescriptorError,
    GrpcDescriptorIndex,
    GrpcMethod,
)
from ..profile.dubbo import scan_dubbo_sources
from ..profile.javatypes import ContractIndex, TypeContract, split_type
from ..profile.langroutes import (
    LanguageRouteScanner,
    _annotation_constraints,
    _java_openapi_type,
)

CONTRACT_MODEL_SCHEMA_VERSION = 1
MAX_CONTRACT_FILES = 2_000
MAX_CONTRACT_FILE_BYTES = 2 * 1024 * 1024
MAX_CONTRACT_FIELDS = 2_000
MAX_CONTRACT_DEPTH = 8
_SKIP_DIRS = {
    ".git",
    ".astsight",
    ".codegraph",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "target",
    "build",
    "dist",
}

SOURCE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "openapi": {
        "transport": "http",
        "supports": [
            "operation",
            "parameter",
            "request_body",
            "response_status",
            "schema",
            "required",
            "type",
            "enum",
            "min",
            "max",
            "format",
        ],
        "gaps": ["external_ref_unresolved", "runtime_validation_unverified"],
    },
    "spring_mvc": {
        "transport": "http",
        "supports": [
            "route",
            "path_parameter",
            "query_parameter",
            "request_body",
            "response_dto",
            "type",
            "common_validation_annotations",
        ],
        "gaps": [
            "runtime_binding_unverified",
            "custom_converter_unverified",
            "serialization_config_unverified",
            "validation_group_unverified",
            "global_exception_mapping_unverified",
        ],
    },
    "grpc_descriptor": {
        "transport": "grpc",
        "supports": [
            "unary_service_method",
            "request_message",
            "response_message",
            "field_type",
            "repeated",
            "optional",
            "enum",
            "oneof",
        ],
        "gaps": [
            "metadata_semantics_unverified",
            "business_error_unverified",
            "runtime_validation_unverified",
            "provider_registration_unverified",
        ],
    },
    "dubbo_interface": {
        "transport": "dubbo",
        "supports": [
            "service_method",
            "parameter_type",
            "return_type",
            "source_dto_field",
            "enum",
            "common_validation_annotations",
        ],
        "gaps": [
            "jar_only_interface_unsupported",
            "dynamic_proxy_unsupported",
            "serialization_customization_unverified",
            "generic_type_ambiguity",
        ],
    },
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


@dataclass(frozen=True)
class SourceRef:
    path: str
    line: int = 0
    symbol: str = ""
    version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "version": self.version,
        }


@dataclass(frozen=True)
class ContractGap:
    source_kind: str
    endpoint: str
    code: str
    detail: str
    field: str = ""
    path: str = ""
    line: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "endpoint": self.endpoint,
            "field": self.field,
            "code": self.code,
            "detail": self.detail,
            "path": self.path,
            "line": self.line,
        }


@dataclass
class ContractField:
    location: str
    name: str
    type: str
    required: bool = False
    constraints: dict[str, Any] = field(default_factory=dict)
    source_kind: str = ""
    source_refs: list[SourceRef] = field(default_factory=list)
    evidence: str = "verified"

    @property
    def path(self) -> str:
        prefix = f"{self.location}." if self.location else ""
        return f"{prefix}{self.name}".strip(".")

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "location": self.location,
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "constraints": dict(self.constraints),
            "source_kind": self.source_kind,
            "source_refs": [item.as_dict() for item in self.source_refs],
            "evidence": self.evidence,
        }


@dataclass
class ContractOperation:
    transport: str
    endpoint: str
    source_kind: str
    source_refs: list[SourceRef] = field(default_factory=list)
    operation_id: str = ""
    request: list[ContractField] = field(default_factory=list)
    response: list[ContractField] = field(default_factory=list)
    success_codes: list[int] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    gap_codes: list[str] = field(default_factory=list)
    contract_eligible: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "endpoint": self.endpoint,
            "operation_id": self.operation_id,
            "source_kind": self.source_kind,
            "source_refs": [item.as_dict() for item in self.source_refs],
            "request": [item.as_dict() for item in self.request],
            "response": [item.as_dict() for item in self.response],
            "success_codes": list(self.success_codes),
            "capabilities": list(self.capabilities),
            "gap_codes": list(self.gap_codes),
            "contract_eligible": self.contract_eligible,
        }


@dataclass
class ContractModel:
    schema_version: int = CONTRACT_MODEL_SCHEMA_VERSION
    operations: list[ContractOperation] = field(default_factory=list)
    gaps: list[ContractGap] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)

    def normalise(self) -> "ContractModel":
        self.operations.sort(
            key=lambda item: (
                item.transport,
                item.endpoint,
                item.source_kind,
            )
        )
        for operation in self.operations:
            operation.request.sort(key=lambda item: item.path)
            operation.response.sort(key=lambda item: item.path)
            operation.source_refs.sort(
                key=lambda item: (item.path, item.line, item.symbol)
            )
        unique: dict[tuple[str, str, str, str, int], ContractGap] = {}
        for gap in self.gaps:
            key = (
                gap.source_kind,
                gap.endpoint,
                gap.field,
                gap.code,
                gap.line,
            )
            unique.setdefault(key, gap)
        self.gaps = sorted(
            unique.values(),
            key=lambda item: (
                item.source_kind,
                item.endpoint,
                item.field,
                item.code,
                item.path,
                item.line,
            ),
        )
        self.sources.sort(key=lambda item: str(item.get("kind") or ""))
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sources": list(self.sources),
            "operations": [item.as_dict() for item in self.operations],
            "gaps": [item.as_dict() for item in self.gaps],
            "counts": {
                "sources": len(self.sources),
                "operations": len(self.operations),
                "request_fields": sum(
                    len(item.request) for item in self.operations
                ),
                "response_fields": sum(
                    len(item.response) for item in self.operations
                ),
                "gaps": len(self.gaps),
            },
        }

    def content_hash(self) -> str:
        return _hash(self.as_dict())


def _source_summary(
    kind: str,
    status: str,
    *,
    files: Iterable[str] = (),
    note: str = "",
) -> dict[str, Any]:
    capability = SOURCE_CAPABILITIES[kind]
    return {
        "kind": kind,
        "transport": capability["transport"],
        "status": status,
        "files": sorted({str(item) for item in files if item}),
        "supports": list(capability["supports"]),
        "known_gaps": list(capability["gaps"]),
        "note": note,
    }


def _source_boundary_gaps(
    operations: Iterable[ContractOperation],
) -> list[ContractGap]:
    """为每种源的已知表达力边界生成端点级 gap，不生成字段级猜测。"""
    result: list[ContractGap] = []
    for operation in operations:
        if not operation.gap_codes:
            continue
        source_ref = (
            operation.source_refs[0]
            if operation.source_refs
            else SourceRef(path="")
        )
        result.append(
            ContractGap(
                source_kind=operation.source_kind,
                endpoint=operation.endpoint,
                code=f"{operation.source_kind}_runtime_semantics_unverified",
                detail="; ".join(operation.gap_codes),
                path=source_ref.path,
                line=source_ref.line,
            )
        )
    return result


def _constraint_text(kind: str, constraints: dict[str, Any]) -> str:
    parts = [f"type={kind}"] if kind else []
    for key in sorted(constraints):
        value = constraints[key]
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return "; ".join(parts)


def constraint_text(field_value: ContractField) -> str:
    constraints = dict(field_value.constraints)
    if field_value.required:
        constraints["required"] = True
    return _constraint_text(field_value.type, constraints)


def _flatten_json_schema(
    schema: dict[str, Any],
    *,
    location: str,
    prefix: str,
    source_kind: str,
    source_ref: SourceRef,
    depth: int = 0,
) -> list[ContractField]:
    result: list[ContractField] = []
    if depth >= MAX_CONTRACT_DEPTH or len(result) >= MAX_CONTRACT_FIELDS:
        return result
    if not isinstance(schema, dict):
        return result
    kind = str(schema.get("type") or "")
    if kind == "object" or "properties" in schema:
        required = {str(item) for item in schema.get("required") or []}
        for name, child in sorted((schema.get("properties") or {}).items()):
            if not isinstance(child, dict):
                continue
            child_type = str(child.get("type") or "object")
            constraints = {
                key: child[key]
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
                if child.get(key) is not None
            }
            result.append(
                ContractField(
                    location=location,
                    name=f"{prefix}.{name}" if prefix else str(name),
                    type=child_type,
                    required=str(name) in required,
                    constraints=constraints,
                    source_kind=source_kind,
                    source_refs=[source_ref],
                )
            )
            result.extend(
                _flatten_json_schema(
                    child,
                    location=location,
                    prefix=f"{prefix}.{name}" if prefix else str(name),
                    source_kind=source_kind,
                    source_ref=source_ref,
                    depth=depth + 1,
                )
            )
            if len(result) >= MAX_CONTRACT_FIELDS:
                break
    elif kind == "array" and isinstance(schema.get("items"), dict):
        result.extend(
            _flatten_json_schema(
                schema["items"],
                location=location,
                prefix=f"{prefix}[]" if prefix else "[]",
                source_kind=source_kind,
                source_ref=source_ref,
                depth=depth + 1,
            )
        )
    return result


def _openapi_operations(
    operations: list[Any],
    *,
    source: str,
    version: str,
) -> list[ContractOperation]:
    result: list[ContractOperation] = []
    for operation in operations:
        endpoint = str(getattr(operation, "endpoint", "") or "")
        source_ref = SourceRef(
            path=source or "openapi",
            symbol=endpoint,
            version=version,
        )
        request: list[ContractField] = []
        for param in getattr(operation, "params", []) or []:
            name = str(getattr(param, "name", "") or "")
            if not name:
                continue
            constraints = dict(getattr(param, "constraints", {}) or {})
            request.append(
                ContractField(
                    location=str(getattr(param, "location", "query") or "query"),
                    name=name,
                    type=str(getattr(param, "type", "string") or "string"),
                    required=bool(getattr(param, "required", False)),
                    constraints=constraints,
                    source_kind="openapi",
                    source_refs=[source_ref],
                )
            )
        body_schema = getattr(operation, "body_schema", {}) or {}
        request.extend(
            _flatten_json_schema(
                body_schema,
                location="body",
                prefix="",
                source_kind="openapi",
                source_ref=source_ref,
            )
        )
        response: list[ContractField] = []
        for code, fields in sorted(
            (getattr(operation, "response_fields", {}) or {}).items()
        ):
            for name in fields:
                response.append(
                    ContractField(
                        location=f"response.{code}",
                        name=str(name),
                        type="",
                        source_kind="openapi",
                        source_refs=[source_ref],
                    )
                )
        result.append(
            ContractOperation(
                transport="http",
                endpoint=endpoint,
                operation_id=str(getattr(operation, "operation_id", "") or ""),
                source_kind="openapi",
                source_refs=[source_ref],
                request=request,
                response=response,
                success_codes=list(getattr(operation, "success_codes", []) or []),
                capabilities=list(SOURCE_CAPABILITIES["openapi"]["supports"]),
                gap_codes=list(SOURCE_CAPABILITIES["openapi"]["gaps"]),
            )
        )
    return result


def _project_files(project: Path, suffixes: set[str]) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(project.rglob("*")):
        if len(files) >= MAX_CONTRACT_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(project)
        except (OSError, ValueError):
            continue
        try:
            if resolved.stat().st_size > MAX_CONTRACT_FILE_BYTES:
                continue
        except OSError:
            continue
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        files.append((resolved, resolved.suffix.lower()))
    return files


def _java_fields(
    index: ContractIndex,
    project: Path,
    type_expr: str,
    *,
    location: str,
    prefix: str,
    source_kind: str,
    source_ref: SourceRef,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> tuple[list[ContractField], list[ContractGap]]:
    fields: list[ContractField] = []
    gaps: list[ContractGap] = []
    if depth >= MAX_CONTRACT_DEPTH or not type_expr:
        return fields, gaps
    base, container, item = split_type(type_expr)
    target = item if container in {"list", "page"} and item else base
    contract = index.resolve(target, context_file=source_ref.path)
    if contract is None:
        gaps.append(
            ContractGap(
                source_kind=source_kind,
                endpoint=source_ref.symbol,
                field=prefix,
                code=f"{source_kind}_type_unresolved",
                detail=type_expr,
                path=source_ref.path,
                line=source_ref.line,
            )
        )
        return fields, gaps
    if container in {"list", "page"}:
        prefix = f"{prefix}[]" if prefix else "[]"
    for item_field in contract.fields:
        constraints, required = _annotation_constraints(
            list(item_field.annotations)
        )
        path = f"{prefix}.{item_field.name}" if prefix else item_field.name
        field_type = _java_openapi_type(item_field.type)
        field_source = SourceRef(
            path=contract.origin,
            symbol=f"{contract.name}.{item_field.name}",
        )
        fields.append(
            ContractField(
                location=location,
                name=path,
                type=field_type,
                required=required,
                constraints=constraints,
                source_kind=source_kind,
                source_refs=[field_source],
            )
        )
        child_type = item_field.type
        child_base, child_container, child_item = split_type(child_type)
        if child_container in {"list", "page"} and child_item:
            child_type = child_item
        else:
            child_type = child_base
        if (
            child_type
            and child_type not in seen
            and _java_openapi_type(child_type) == "object"
        ):
            nested, nested_gaps = _java_fields(
                index,
                project,
                item_field.type,
                location=location,
                prefix=path,
                source_kind=source_kind,
                source_ref=SourceRef(
                    path=contract.origin,
                    symbol=field_source.symbol,
                ),
                depth=depth + 1,
                seen=seen | {child_type},
            )
            fields.extend(nested)
            gaps.extend(nested_gaps)
        if len(fields) >= MAX_CONTRACT_FIELDS:
            break
    return fields, gaps


def _spring_operations(
    project: Path,
) -> tuple[list[ContractOperation], list[ContractGap], list[str], list[str]]:
    scanner = LanguageRouteScanner(project)
    routes = scanner.scan()
    index = ContractIndex(project)
    operations: list[ContractOperation] = []
    gaps: list[ContractGap] = []
    files: list[str] = []
    warnings = list(scanner.warnings)
    for route in routes:
        if route.framework != "spring":
            continue
        endpoint = f"{route.method} {route.path}"
        source_ref = SourceRef(
            path=route.file,
            line=route.line,
            symbol=f"{route.handler_owner}.{route.handler}",
        )
        files.append(route.file)
        request: list[ContractField] = []
        for parameter in route.parameters:
            location = str(parameter.get("in") or "")
            name = str(parameter.get("name") or "")
            java_type = str(parameter.get("java_type") or "")
            constraints = dict(parameter.get("constraints") or {})
            if location == "body" and java_type:
                body_fields, body_gaps = _java_fields(
                    index,
                    project,
                    java_type,
                    location="body",
                    prefix="",
                    source_kind="spring_mvc",
                    source_ref=SourceRef(
                        path=route.file,
                        line=route.line,
                        symbol=source_ref.symbol,
                    ),
                    seen=frozenset({java_type}),
                )
                request.extend(body_fields)
                gaps.extend(body_gaps)
            else:
                request.append(
                    ContractField(
                        location=location,
                        name=name,
                        type=str(parameter.get("type") or "object"),
                        required=bool(parameter.get("required", False)),
                        constraints=constraints,
                        source_kind="spring_mvc",
                        source_refs=[source_ref],
                    )
                )
        response: list[ContractField] = []
        if route.response_type:
            response, response_gaps = _java_fields(
                index,
                project,
                route.response_type,
                location="response",
                prefix="",
                source_kind="spring_mvc",
                source_ref=SourceRef(
                    path=route.file,
                    line=route.line,
                    symbol=source_ref.symbol,
                ),
                seen=frozenset({route.response_type}),
            )
            gaps.extend(response_gaps)
        runtime_gaps = [
            "runtime_binding_unverified",
            "custom_converter_unverified",
            "serialization_config_unverified",
            "validation_group_unverified",
            "global_exception_mapping_unverified",
        ]
        for gap in route.contract_gaps:
            gaps.append(
                ContractGap(
                    source_kind="spring_mvc",
                    endpoint=endpoint,
                    code=str(gap.get("code") or "spring_contract_unverified"),
                    detail=str(gap.get("detail") or ""),
                    path=route.file,
                    line=route.line,
                )
            )
        operations.append(
            ContractOperation(
                transport="http",
                endpoint=endpoint,
                operation_id=route.handler,
                source_kind="spring_mvc",
                source_refs=[source_ref],
                request=request,
                response=response,
                capabilities=list(SOURCE_CAPABILITIES["spring_mvc"]["supports"]),
                gap_codes=runtime_gaps,
            )
        )
    warnings.extend(index.warnings)
    return operations, gaps, files, warnings


def _flatten_dubbo_schema(
    schema: dict[str, Any],
    *,
    location: str,
    prefix: str,
    source_ref: SourceRef,
    endpoint: str,
    depth: int = 0,
) -> tuple[list[ContractField], list[ContractGap]]:
    fields: list[ContractField] = []
    gaps: list[ContractGap] = []
    if not isinstance(schema, dict) or depth >= MAX_CONTRACT_DEPTH:
        return fields, gaps
    kind = str(schema.get("kind") or "unknown")
    constraints = dict(schema.get("constraints") or {})
    schema_type = str(schema.get("type") or "")
    if kind == "opaque":
        gaps.append(
            ContractGap(
                source_kind="dubbo_interface",
                endpoint=endpoint,
                field=prefix,
                code="dubbo_type_opaque",
                detail=schema_type,
                path=source_ref.path,
                line=source_ref.line,
            )
        )
        return fields, gaps
    if kind in {"scalar", "temporal", "enum"}:
        if kind == "enum":
            constraints.setdefault("enum", list(schema.get("enum_values") or []))
        fields.append(
            ContractField(
                location=location,
                name=prefix or "value",
                type="string" if kind in {"temporal", "enum"} else _java_openapi_type(schema_type),
                constraints=constraints,
                source_kind="dubbo_interface",
                source_refs=[source_ref],
            )
        )
        return fields, gaps
    if kind in {"collection", "array", "map"}:
        child = schema.get("element") or schema.get("value")
        field_type = "array" if kind in {"collection", "array"} else "object"
        fields.append(
            ContractField(
                location=location,
                name=prefix or "value",
                type=field_type,
                constraints=constraints,
                source_kind="dubbo_interface",
                source_refs=[source_ref],
            )
        )
        if isinstance(child, dict):
            nested, nested_gaps = _flatten_dubbo_schema(
                child,
                location=location,
                prefix=f"{prefix}[]" if prefix else "[]",
                source_ref=source_ref,
                endpoint=endpoint,
                depth=depth + 1,
            )
            fields.extend(nested)
            gaps.extend(nested_gaps)
        return fields, gaps
    if kind == "reference":
        gaps.append(
            ContractGap(
                source_kind="dubbo_interface",
                endpoint=endpoint,
                field=prefix,
                code="dubbo_recursive_type_unverified",
                detail=schema_type,
                path=source_ref.path,
                line=source_ref.line,
            )
        )
        return fields, gaps
    for item in schema.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        path = f"{prefix}.{name}" if prefix else name
        item_constraints = dict(item.get("constraints") or {})
        child_schema = item.get("schema") or {}
        child_kind = str(child_schema.get("kind") or "")
        item_type = (
            "string"
            if child_kind in {"temporal", "enum"}
            else _java_openapi_type(str(item.get("type") or ""))
        )
        fields.append(
            ContractField(
                location=location,
                name=path,
                type=item_type,
                required=bool(item.get("required", False)),
                constraints=item_constraints,
                source_kind="dubbo_interface",
                source_refs=[source_ref],
            )
        )
        if child_kind in {"dto", "collection", "array", "map", "enum"}:
            nested, nested_gaps = _flatten_dubbo_schema(
                child_schema,
                location=location,
                prefix=path,
                source_ref=source_ref,
                endpoint=endpoint,
                depth=depth + 1,
            )
            fields.extend(
                item
                for item in nested
                if item.path != f"{location}.{path}"
            )
            gaps.extend(nested_gaps)
        if len(fields) >= MAX_CONTRACT_FIELDS:
            break
    return fields, gaps


def _dubbo_operations(
    project: Path,
) -> tuple[list[ContractOperation], list[ContractGap], list[str], list[str]]:
    files = _project_files(
        project,
        {".java", ".kt", ".kts", ".xml", ".properties", ".yml", ".yaml", ".proto"},
    )
    scan = scan_dubbo_sources(
        files=files,
        root=project,
        read_text=lambda path: path.read_text(encoding="utf-8", errors="replace"),
    )
    operations: list[ContractOperation] = []
    gaps: list[ContractGap] = []
    source_files: list[str] = []
    for provider in scan.providers:
        endpoint = str(provider.get("endpoint") or "")
        source_ref = SourceRef(
            path=str(provider.get("file") or ""),
            line=int(provider.get("method_line") or provider.get("line") or 0),
            symbol=str(provider.get("business_key") or endpoint),
        )
        source_files.append(source_ref.path)
        request: list[ContractField] = []
        for index, schema in enumerate(provider.get("parameter_schemas") or []):
            request_fields, request_gaps = _flatten_dubbo_schema(
                schema,
                location="request",
                prefix=f"arg{index}",
                source_ref=source_ref,
                endpoint=endpoint,
            )
            request.extend(request_fields)
            gaps.extend(request_gaps)
        response, response_gaps = _flatten_dubbo_schema(
            provider.get("result_schema") or {},
            location="response",
            prefix="result",
            source_ref=source_ref,
            endpoint=endpoint,
        )
        gaps.extend(response_gaps)
        if provider.get("contract_incomplete"):
            gaps.append(
                ContractGap(
                    source_kind="dubbo_interface",
                    endpoint=endpoint,
                    code=str(
                        provider.get("contract_incomplete_reason")
                        or "dubbo_contract_incomplete"
                    ),
                    detail="Dubbo 返回类型或参数类型无法静态解析",
                    path=source_ref.path,
                    line=source_ref.line,
                )
            )
        operations.append(
            ContractOperation(
                transport="dubbo",
                endpoint=endpoint,
                operation_id=str(provider.get("method") or ""),
                source_kind="dubbo_interface",
                source_refs=[source_ref],
                request=request,
                response=response,
                capabilities=list(
                    SOURCE_CAPABILITIES["dubbo_interface"]["supports"]
                ),
                gap_codes=list(
                    SOURCE_CAPABILITIES["dubbo_interface"]["gaps"]
                ),
            )
        )
    for item in scan.unsupported:
        detail = str(item.get("detail") or item.get("file") or "")
        gaps.append(
            ContractGap(
                source_kind="dubbo_interface",
                endpoint=f"dubbo:{detail}" if detail else "dubbo:unsupported",
                code=str(item.get("reason") or "dubbo_unsupported"),
                detail=detail,
                path=str(item.get("file") or ""),
                line=int(item.get("line") or 0),
            )
        )
    return operations, gaps, source_files, list(scan.warnings)


_PROTO_SCALARS = {
    1: "number",
    2: "number",
    3: "integer",
    4: "integer",
    5: "integer",
    6: "integer",
    7: "integer",
    8: "boolean",
    9: "string",
    10: "object",
    11: "object",
    12: "string",
    13: "integer",
    14: "string",
    15: "integer",
    16: "integer",
    17: "integer",
    18: "integer",
}


def _grpc_message_fields(
    index: GrpcDescriptorIndex,
    type_name: str,
    *,
    location: str,
    prefix: str,
    source_ref: SourceRef,
    endpoint: str,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> tuple[list[ContractField], list[ContractGap]]:
    fields: list[ContractField] = []
    gaps: list[ContractGap] = []
    if depth >= MAX_CONTRACT_DEPTH or type_name in seen:
        return fields, gaps
    try:
        message = index.pool.FindMessageTypeByName(type_name)
    except KeyError:
        gaps.append(
            ContractGap(
                source_kind="grpc_descriptor",
                endpoint=endpoint,
                field=prefix,
                code="grpc_message_not_found",
                detail=type_name,
                path=source_ref.path,
            )
        )
        return fields, gaps
    for field in message.fields:
        if field.message_type is not None and field.message_type.GetOptions().map_entry:
            fields.append(
                ContractField(
                    location=location,
                    name=f"{prefix}.{field.json_name}" if prefix else field.json_name,
                    type="object",
                    constraints={"map": True},
                    source_kind="grpc_descriptor",
                    source_refs=[source_ref],
                )
            )
            continue
        constraints: dict[str, Any] = {}
        if field.is_repeated:
            constraints["repeated"] = True
        if (
            field.containing_oneof is not None
            and not field.containing_oneof.name.startswith("_")
        ):
            constraints["oneof"] = field.containing_oneof.name
        field_type = _PROTO_SCALARS.get(field.type, "object")
        if field.enum_type is not None:
            field_type = "string"
            constraints["enum"] = [item.name for item in field.enum_type.values]
        if field.message_type is not None:
            field_type = "object"
        required = bool(getattr(field, "is_required", False))
        path = f"{prefix}.{field.json_name}" if prefix else field.json_name
        fields.append(
            ContractField(
                location=location,
                name=path,
                type=field_type,
                required=required,
                constraints=constraints,
                source_kind="grpc_descriptor",
                source_refs=[source_ref],
            )
        )
        if field.message_type is not None:
            nested, nested_gaps = _grpc_message_fields(
                index,
                field.message_type.full_name,
                location=location,
                prefix=f"{path}[]" if field.is_repeated else path,
                source_ref=source_ref,
                endpoint=endpoint,
                depth=depth + 1,
                seen=seen | {type_name},
            )
            fields.extend(nested)
            gaps.extend(nested_gaps)
        if len(fields) >= MAX_CONTRACT_FIELDS:
            break
    return fields, gaps


def _grpc_provider_texts(project: Path) -> list[str]:
    texts: list[str] = []
    for path, _suffix in _project_files(
        project,
        {".py", ".java", ".kt", ".go", ".ts", ".js"},
    ):
        relative = path.relative_to(project).as_posix()
        if (
            relative.startswith("test/")
            or relative.startswith("tests/")
            or "/test/" in relative
            or "/tests/" in relative
        ):
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def _grpc_provider_hint(texts: list[str], service: str) -> bool:
    short = service.rsplit(".", 1)[-1]
    patterns = (
        f"add_{short}Servicer_to_server",
        f"{short}Grpc.{short}ImplBase",
        f"extends {short}Grpc",
    )
    for text in texts:
        if any(pattern in text for pattern in patterns):
            return True
    return False


def _grpc_operations(
    project: Path,
) -> tuple[list[ContractOperation], list[ContractGap], list[str], list[str]]:
    operations: list[ContractOperation] = []
    gaps: list[ContractGap] = []
    source_files: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    provider_texts = _grpc_provider_texts(project)
    for path, _suffix in _project_files(project, {".proto", ".protoset"}):
        rel = path.relative_to(project).as_posix()
        try:
            index = GrpcDescriptorIndex.load(path)
        except (OSError, GrpcDescriptorError) as exc:
            code = f"grpc_descriptor_{getattr(exc, 'code', type(exc).__name__)}"
            warnings.append(f"grpc_descriptor_unavailable:{rel}:{code}")
            gaps.append(
                ContractGap(
                    source_kind="grpc_descriptor",
                    endpoint="grpc:descriptor",
                    code=code,
                    detail=str(exc),
                    path=rel,
                )
            )
            continue
        source_files.append(rel)
        for method in index.all_methods():
            if method.grpc_path in seen:
                continue
            seen.add(method.grpc_path)
            endpoint = f"grpc:{method.grpc_path}"
            source_ref = SourceRef(
                path=rel,
                symbol=f"{method.service}/{method.method}",
                version=_file_hash(path),
            )
            if method.client_streaming or method.server_streaming:
                gaps.append(
                    ContractGap(
                        source_kind="grpc_descriptor",
                        endpoint=endpoint,
                        code="grpc_streaming_unsupported",
                        detail="仅 unary 方法可确定性生成 CR",
                        path=rel,
                    )
                )
                continue
            request, request_gaps = _grpc_message_fields(
                index,
                method.request_type,
                location="request",
                prefix="",
                source_ref=source_ref,
                endpoint=endpoint,
            )
            response, response_gaps = _grpc_message_fields(
                index,
                method.response_type,
                location="response",
                prefix="",
                source_ref=source_ref,
                endpoint=endpoint,
            )
            gaps.extend(request_gaps)
            gaps.extend(response_gaps)
            provider_verified = _grpc_provider_hint(
                provider_texts,
                method.service,
            )
            if not provider_verified:
                gaps.append(
                    ContractGap(
                        source_kind="grpc_descriptor",
                        endpoint=endpoint,
                        code="grpc_provider_registration_unverified",
                        detail="descriptor 存在，但未定位到该 service 的提供方注册",
                        path=rel,
                    )
                )
            operations.append(
                ContractOperation(
                    transport="grpc",
                    endpoint=endpoint,
                    operation_id=method.method,
                    source_kind="grpc_descriptor",
                    source_refs=[source_ref],
                    request=request,
                    response=response,
                    capabilities=list(
                        SOURCE_CAPABILITIES["grpc_descriptor"]["supports"]
                    ),
                    gap_codes=[str(item) for item in SOURCE_CAPABILITIES["grpc_descriptor"]["gaps"]],
                    contract_eligible=provider_verified,
                )
            )
    return operations, gaps, source_files, warnings


def build_contract_model(
    project: str | Path,
    *,
    openapi_operations: list[Any] | None = None,
    openapi_source: str = "",
    openapi_version: str = "",
    openapi_gaps: Iterable[ContractGap] = (),
) -> ContractModel:
    """扫描项目并构建规范化契约模型；不调用模型、不执行被测代码。"""
    root = Path(project).resolve()
    model = ContractModel()
    sources: list[dict[str, Any]] = []

    openapi_ops = _openapi_operations(
        list(openapi_operations or []),
        source=openapi_source,
        version=openapi_version,
    )
    model.operations.extend(openapi_ops)
    model.gaps.extend(_source_boundary_gaps(openapi_ops))
    model.gaps.extend(openapi_gaps)
    sources.append(
        _source_summary(
            "openapi",
            "available" if openapi_ops else "missing",
            files=[openapi_source] if openapi_source else [],
        )
    )

    spring_ops, spring_gaps, spring_files, spring_warnings = _spring_operations(root)
    model.operations.extend(spring_ops)
    model.gaps.extend(spring_gaps)
    model.gaps.extend(_source_boundary_gaps(spring_ops))
    sources.append(
        _source_summary(
            "spring_mvc",
            "available" if spring_ops else "missing",
            files=spring_files,
            note="; ".join(spring_warnings[:10]),
        )
    )

    dubbo_ops, dubbo_gaps, dubbo_files, dubbo_warnings = _dubbo_operations(root)
    model.operations.extend(dubbo_ops)
    model.gaps.extend(dubbo_gaps)
    model.gaps.extend(_source_boundary_gaps(dubbo_ops))
    sources.append(
        _source_summary(
            "dubbo_interface",
            "available" if dubbo_ops else ("partial" if dubbo_gaps else "missing"),
            files=dubbo_files,
            note="; ".join(dubbo_warnings[:10]),
        )
    )

    grpc_ops, grpc_gaps, grpc_files, grpc_warnings = _grpc_operations(root)
    model.operations.extend(grpc_ops)
    model.gaps.extend(grpc_gaps)
    model.gaps.extend(_source_boundary_gaps(grpc_ops))
    sources.append(
        _source_summary(
            "grpc_descriptor",
            "available" if grpc_ops else ("partial" if grpc_gaps else "missing"),
            files=grpc_files,
            note="; ".join(grpc_warnings[:10]),
        )
    )

    model.sources = sources
    return model.normalise()


def contract_rules_from_model(
    model: ContractModel | dict[str, Any],
) -> list[dict[str, Any]]:
    """把 ContractModel 展开成待构造 CR 的确定性记录。"""
    payload = model if isinstance(model, dict) else model.as_dict()
    records: dict[str, dict[str, Any]] = {}
    operations = payload.get("operations") or []
    for operation in operations:
        if not operation.get("contract_eligible", True):
            continue
        endpoint = str(operation.get("endpoint") or "")
        transport = str(operation.get("transport") or "")
        source_kind = str(operation.get("source_kind") or "")
        refs = list(operation.get("source_refs") or [])
        for field_value in [
            *(operation.get("request") or []),
            *(operation.get("response") or []),
        ]:
            constraint = _constraint_text(
                str(field_value.get("type") or ""),
                {
                    **dict(field_value.get("constraints") or {}),
                    **(
                        {"required": True}
                        if field_value.get("required")
                        else {}
                    ),
                },
            )
            if not constraint:
                continue
            field_path = str(field_value.get("path") or "")
            key = _canonical([transport, endpoint, field_path, constraint])
            support = {
                "source_kind": source_kind,
                "source_refs": refs,
            }
            current = records.get(key)
            if current is None:
                records[key] = {
                    "transport": transport,
                    "endpoint": endpoint,
                    "field": field_path,
                    "constraint": constraint,
                    "source_kind": source_kind,
                    "support": [support],
                }
            elif support not in current["support"]:
                current["support"].append(support)
    result = list(records.values())
    for item in result:
        item["support"].sort(
            key=lambda support: (
                str(support.get("source_kind") or ""),
                _canonical(support.get("source_refs") or []),
            )
        )
    result.sort(
        key=lambda item: (
            item["endpoint"],
            item["field"],
            item["constraint"],
        )
    )
    return result


def context_contract_summary(model: ContractModel) -> dict[str, Any]:
    return {
        "model_hash": model.content_hash(),
        "counts": model.as_dict()["counts"],
        "sources": list(model.sources),
        "gap_codes": sorted({gap.code for gap in model.gaps}),
    }


__all__ = [
    "CONTRACT_MODEL_SCHEMA_VERSION",
    "SOURCE_CAPABILITIES",
    "ContractField",
    "ContractGap",
    "ContractModel",
    "ContractOperation",
    "SourceRef",
    "build_contract_model",
    "constraint_text",
    "context_contract_summary",
    "contract_rules_from_model",
]
