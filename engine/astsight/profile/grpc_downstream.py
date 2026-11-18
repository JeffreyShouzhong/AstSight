"""Java/Python gRPC downstream 静态画像。"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..mock.grpc_descriptor import GrpcDescriptorError, GrpcDescriptorIndex, GrpcMethod

_SKIP_DIRS = {
    ".git",
    ".astsight",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
}
_MAX_FILES = 5_000
_MAX_FILE_BYTES = 1_048_576
_MAX_PROTO_FILES = 2_000


@dataclass
class GrpcCallSite:
    service: str
    method: str
    file: str
    line: int
    stub: str = ""
    target: str | None = None
    property: str | None = None
    config_key: str | None = None
    config_kind: str | None = None
    language: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "method": self.method,
            "file": self.file,
            "line": self.line,
            "stub": self.stub,
            "target": self.target,
            "property": self.property,
            "config_key": self.config_key,
            "config_kind": self.config_kind,
            "language": self.language,
        }


@dataclass
class GrpcDownstreamDep:
    grpc_path: str
    service: str
    method: str
    request_type: str
    response_type: str
    descriptor_source: str
    response_schema: Any = None
    target: str | None = None
    target_property: str | None = None
    target_config_key: str | None = None
    target_config_kind: str | None = None
    call_sites: list[GrpcCallSite] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)

    @property
    def dep_id(self) -> str:
        return f"grpc:{self.grpc_path}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "dep_id": self.dep_id,
            "dep_type": "grpc",
            "transport": "grpc",
            "grpc_path": self.grpc_path,
            "service": self.service,
            "method": self.method,
            "request_type": self.request_type,
            "response_type": self.response_type,
            "descriptor_source": self.descriptor_source,
            "response_schema": self.response_schema,
            "target": self.target,
            "property": self.target_property,
            "config_key": self.target_config_key,
            "config_kind": self.target_config_kind,
            "call_sites": [item.as_dict() for item in self.call_sites],
            "languages": sorted(set(self.languages)),
            "evidence": "verified",
        }


@dataclass
class GrpcDownstreamScan:
    deps: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: bool = False


def _files(root: Path, suffixes: set[str], limit: int) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if len(found) >= limit:
            break
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return found


def _env_key(property_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", property_name).strip("_").upper()


def _expr_value(node: ast.AST) -> tuple[str | None, str | None]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, None
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _expr_value(node.elts[0])
    if isinstance(node, ast.Subscript):
        if _attr_name(node.value) in {"os.environ", "environ"}:
            key, _ = _expr_value(node.slice)
            if key:
                return None, key
    if isinstance(node, ast.Call):
        name = _attr_name(node.func)
        if name in {"os.environ.get", "os.getenv", "environ.get"}:
            key = _expr_value(node.args[0])[0] if node.args else None
            default = (
                _expr_value(node.args[1])[0]
                if len(node.args) > 1
                else None
            )
            if key:
                return default, key
    return None, None


def _attr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attr_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _python_callsites(
    root: Path,
    proto_methods: dict[str, dict[str, GrpcMethod]],
) -> tuple[list[GrpcCallSite], list[dict[str, Any]], list[str]]:
    sites: list[GrpcCallSite] = []
    unsupported: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in _files(root, {".py"}, _MAX_FILES):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError) as exc:
            warnings.append(f"grpc_python_parse_failed:{path.relative_to(root)}:{exc}")
            continue
        stubs: dict[str, str] = {}
        channels: dict[str, tuple[str | None, str | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not isinstance(value, ast.Call):
                    continue
                call_name = _attr_name(value.func)
                for target in targets:
                    if isinstance(target, ast.Name):
                        if call_name.rsplit(".", 1)[-1].endswith("Stub"):
                            stubs[target.id] = call_name.rsplit(".", 1)[-1]
                        elif call_name.endswith("insecure_channel"):
                            channels[target.id] = _expr_value(value.args[0]) if value.args else (None, None)
                        elif call_name.endswith("secure_channel"):
                            channels[target.id] = _expr_value(value.args[0]) if value.args else (None, None)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in stubs:
                continue
            stub_class = stubs[owner.id]
            service_short = stub_class[: -len("Stub")]
            methods = proto_methods.get(service_short)
            if not methods:
                unsupported.append(
                    {
                        "kind": "grpc",
                        "reason": "grpc_service_unresolved",
                        "detail": f"{path.relative_to(root)}:{node.lineno}:{stub_class}",
                    }
                )
                continue
            method = _match_method(methods, node.func.attr)
            if method is None:
                unsupported.append(
                    {
                        "kind": "grpc",
                        "reason": "grpc_method_unresolved",
                        "detail": f"{path.relative_to(root)}:{node.lineno}:{node.func.attr}",
                    }
                )
                continue
            target = property_name = None
            channel_var = None
            for source_node in ast.walk(tree):
                if (
                    isinstance(source_node, (ast.Assign, ast.AnnAssign))
                    and isinstance(
                        source_node.targets[0]
                        if isinstance(source_node, ast.Assign)
                        else source_node.target,
                        ast.Name,
                    )
                ):
                    name = (
                        source_node.targets[0].id
                        if isinstance(source_node, ast.Assign)
                        else source_node.target.id
                    )
                    if name == owner.id:
                        init = source_node.value
                        if isinstance(init, ast.Call) and init.args:
                            candidate = init.args[0]
                            if isinstance(candidate, ast.Name):
                                channel_var = candidate.id
            if channel_var:
                target, property_name = channels.get(channel_var, (None, None))
            sites.append(
                GrpcCallSite(
                    service=method.service,
                    method=method.method,
                    file=str(path.relative_to(root)).replace("\\", "/"),
                    line=node.lineno,
                    stub=stub_class,
                    target=target,
                    property=property_name,
                    config_kind="env" if property_name else None,
                    language="python",
                )
            )
    return sites, unsupported, warnings


def _match_method(methods: dict[str, GrpcMethod], called: str) -> GrpcMethod | None:
    normalized = re.sub(r"[^a-z0-9]", "", called.lower())
    for name, method in methods.items():
        if re.sub(r"[^a-z0-9]", "", name.lower()) == normalized:
            return method
    return None


def _java_callsites(
    root: Path,
    proto_methods: dict[str, dict[str, GrpcMethod]],
) -> tuple[list[GrpcCallSite], list[dict[str, Any]], list[str]]:
    sites: list[GrpcCallSite] = []
    unsupported: list[dict[str, Any]] = []
    warnings: list[str] = []
    stub_re = re.compile(r"\b([A-Za-z0-9_]+)Stub\s+([A-Za-z_][A-Za-z0-9_]*)")
    prop_re = re.compile(
        r"@Value\s*\(\s*(['\"])\$\{([^}:]+)(?::[^}]*)?\}\1\s*\)"
    )
    getenv_re = re.compile(
        r"System\s*\.\s*getenv\s*\(\s*\"([^\"]+)\"\s*\)"
    )
    getprop_re = re.compile(
        r"System\s*\.\s*getProperty\s*\(\s*\"([^\"]+)\""
    )
    target_re = re.compile(
        r"ManagedChannelBuilder\s*\.\s*(?:forAddress|forTarget)\s*\(\s*\"([^\"]+)\""
    )
    for path in _files(root, {".java"}, _MAX_FILES):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        bindings: list[tuple[str, str | None, str]] = []
        for match in prop_re.finditer(source):
            key = match.group(2).strip()
            if key:
                bindings.append((_env_key(key), key, "spring_property"))
        for key in getenv_re.findall(source):
            bindings.append((key, None, "env"))
        for key in getprop_re.findall(source):
            bindings.append((_env_key(key), key, "system_property"))
        bindings = list(dict.fromkeys(bindings))
        targets = target_re.findall(source)
        for match in stub_re.finditer(source):
            service_short = re.sub(
                r"(Blocking|Future)$", "", match.group(1)
            )
            variable = match.group(2)
            methods = proto_methods.get(service_short)
            if not methods:
                continue
            call_re = re.compile(
                rf"\b{re.escape(variable)}\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\("
            )
            for call in call_re.finditer(source):
                method = _match_method(methods, call.group(1))
                if method is None:
                    continue
                sites.append(
                    GrpcCallSite(
                        service=method.service,
                        method=method.method,
                        file=str(path.relative_to(root)).replace("\\", "/"),
                        line=source.count("\n", 0, call.start()) + 1,
                        stub=f"{service_short}Stub",
                        target=targets[0] if len(set(targets)) == 1 else None,
                        property=(
                            bindings[0][0] if len(bindings) == 1 else None
                        ),
                        config_key=(
                            bindings[0][1] if len(bindings) == 1 else None
                        ),
                        config_kind=(
                            bindings[0][2] if len(bindings) == 1 else None
                        ),
                        language="java",
                    )
                )
        if not sites and "_grpc" in source.lower():
            warnings.append(f"grpc_java_stub_unresolved:{path.relative_to(root)}")
    return sites, unsupported, warnings


class GrpcDownstreamScanner:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._result: GrpcDownstreamScan | None = None

    def scan(self) -> GrpcDownstreamScan:
        if self._result is not None:
            return self._result
        proto_paths = _files(self.root, {".proto"}, _MAX_PROTO_FILES)
        include_dirs = sorted({path.parent for path in proto_paths})
        descriptors: list[tuple[str, GrpcDescriptorIndex]] = []
        unsupported: list[dict[str, Any]] = []
        warnings: list[str] = []
        for path in proto_paths:
            try:
                index = GrpcDescriptorIndex.load(path, include_dirs=include_dirs)
            except GrpcDescriptorError as exc:
                unsupported.append(
                    {
                        "kind": "grpc",
                        "reason": exc.code,
                        "detail": f"{path.relative_to(self.root)}:{exc.detail}",
                    }
                )
                continue
            descriptors.append((str(path.relative_to(self.root)).replace("\\", "/"), index))
        by_short: dict[str, dict[str, GrpcMethod]] = {}
        descriptor_by_path: dict[tuple[str, str], str] = {}
        response_samples: dict[str, Any] = {}
        ambiguous: set[str] = set()
        for source, index in descriptors:
            for method in index.unary_methods():
                short = method.service.rsplit(".", 1)[-1]
                methods = by_short.setdefault(short, {})
                if method.method in methods and methods[method.method].grpc_path != method.grpc_path:
                    ambiguous.add(short)
                methods[method.method] = method
                descriptor_by_path[(method.grpc_path, method.method)] = source
                response_samples[method.grpc_path] = index.sample_message(
                    method.response_type
                )
        for short in sorted(ambiguous):
            by_short.pop(short, None)
            unsupported.append(
                {
                    "kind": "grpc",
                    "reason": "grpc_service_ambiguous",
                    "detail": short,
                }
            )
        python_sites, py_unsupported, py_warnings = _python_callsites(self.root, by_short)
        java_sites, java_unsupported, java_warnings = _java_callsites(self.root, by_short)
        sites = python_sites + java_sites
        unsupported.extend(py_unsupported)
        unsupported.extend(java_unsupported)
        warnings.extend(py_warnings)
        warnings.extend(java_warnings)
        grouped: dict[str, GrpcDownstreamDep] = {}
        for site in sites:
            key = f"/{site.service}/{site.method}"
            source = descriptor_by_path.get((key, site.method))
            method = by_short[site.service.rsplit(".", 1)[-1]][site.method]
            dep = grouped.get(key)
            if dep is None:
                dep = GrpcDownstreamDep(
                    grpc_path=key,
                    service=method.service,
                    method=method.method,
                    request_type=method.request_type,
                    response_type=method.response_type,
                    descriptor_source=source or "",
                    response_schema=response_samples.get(key),
                )
                grouped[key] = dep
            dep.call_sites.append(site)
            dep.languages.append(site.language)
            if site.target:
                dep.target = site.target if dep.target in (None, site.target) else None
            if site.property:
                dep.target_property = (
                    site.property
                    if dep.target_property in (None, site.property)
                    else None
                )
            if site.config_key:
                dep.target_config_key = (
                    site.config_key
                    if dep.target_config_key in (None, site.config_key)
                    else None
                )
            if site.config_kind:
                dep.target_config_kind = (
                    site.config_kind
                    if dep.target_config_kind in (None, site.config_kind)
                    else None
                )
        for dep in grouped.values():
            target_set = {
                site.target for site in dep.call_sites if site.target
            }
            property_set = {
                site.property for site in dep.call_sites if site.property
            }
            config_key_set = {
                site.config_key for site in dep.call_sites if site.config_key
            }
            config_kind_set = {
                site.config_kind for site in dep.call_sites if site.config_kind
            }
            dep.target = next(iter(target_set)) if len(target_set) == 1 else None
            dep.target_property = (
                next(iter(property_set)) if len(property_set) == 1 else None
            )
            dep.target_config_key = (
                next(iter(config_key_set)) if len(config_key_set) == 1 else None
            )
            dep.target_config_kind = (
                next(iter(config_kind_set)) if len(config_kind_set) == 1 else None
            )
            if len(target_set) > 1:
                warnings.append(f"grpc_target_ambiguous:{dep.dep_id}")
            if len(property_set) > 1:
                warnings.append(f"grpc_target_property_ambiguous:{dep.dep_id}")
            if len(config_key_set) > 1:
                warnings.append(f"grpc_target_config_key_ambiguous:{dep.dep_id}")
        self._result = GrpcDownstreamScan(
            deps=[grouped[key].as_dict() for key in sorted(grouped)],
            unsupported=unsupported,
            warnings=sorted(set(warnings)),
            checked=True,
        )
        return self._result


__all__ = [
    "GrpcCallSite",
    "GrpcDownstreamDep",
    "GrpcDownstreamScan",
    "GrpcDownstreamScanner",
]
