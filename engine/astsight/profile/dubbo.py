"""Java 源码里的 Dubbo 消费端识别（C3.5.3，不执行被测代码）。

只识别能静态证明的标准形态：

- `@DubboReference` / Dubbo 的 `@Reference` 注解字段；
- 接口源码在同一仓库（`interface` 声明），参数类型能解析到 JVM 名；
- 字段上的方法调用能与接口方法签名唯一匹配。

不猜：

- 接口只在依赖 jar、参数类型解析不了、同名重载无法区分 → `unsupported`；
- `ReferenceConfig` 编程式引用、XML `<dubbo:reference>` 暂不支持，显式告警；
- 动态代理 / 反射 / `getBean` 拿到的引用不在文本扫描范围内。

`dep_id` 统一由 `astsight.mock.identity.build_dubbo_dep_id` 计算，参数签名
用于区分重载。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..mock.identity import DepIdError, build_dubbo_dep_id
from .langscan import _call_args, _line_of, _mask_comments, _matching_brace

MAX_FILES = 2_000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_DEP_CHARS = 512
MAX_CALL_SITES = 200

_PRIMITIVES = {
    "boolean",
    "byte",
    "char",
    "short",
    "int",
    "long",
    "float",
    "double",
    "void",
}
_JAVA_LANG = {
    "String",
    "Object",
    "Integer",
    "Long",
    "Short",
    "Byte",
    "Character",
    "Boolean",
    "Float",
    "Double",
    "Number",
    "CharSequence",
    "Class",
    "Throwable",
    "Exception",
    "RuntimeException",
    "Iterable",
}
_DUBBO_REFERENCE_IMPORTS = {
    "org.apache.dubbo.config.annotation.DubboReference",
    "org.apache.dubbo.config.annotation.Reference",
    "com.alibaba.dubbo.config.annotation.Reference",
}
_REFERENCE = re.compile(
    r"@(?P<annotation>DubboReference|Reference)\b"
    r"\s*(?:\((?P<args>[^)]*)\))?"
    r"(?P<decl>[^;{}]*)[;=]"
)
_FIELD = re.compile(
    r"([A-Za-z_$][\w.$]*(?:\s*<[^;{}]*?>)?(?:\s*\[\s*\])*)\s+([A-Za-z_$]\w*)\s*$"
)
_TYPE_DECL = re.compile(
    r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)"
)
_PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
_IMPORT = re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.$*]+)\s*;")
_INTERFACE_DECL = re.compile(
    r"\binterface\s+([A-Za-z_$][\w$]*)[^{;]*\{"
)
_METHOD_DECL = re.compile(
    r"(?m)^\s*(?:(?:public|abstract|default|static|private|protected)\s+)*"
    r"(?:<[^>{}]+>\s*)?"
    r"([A-Za-z_$][\w.$<>\[\], ?]*?)\s+"
    r"([A-Za-z_$]\w*)\s*\(([^)]*)\)\s*(?:throws\s+[\w.,\s]+)?\s*[;{]"
)
_CALL = re.compile(r"\b([A-Za-z_$]\w*)\s*\.\s*([A-Za-z_$]\w*)\s*\(")
_XML_REFERENCE = re.compile(
    r"<dubbo:reference\b[^>]*>|<dubbo:reference\b[^>]*/>", re.S
)
_XML_INTERFACE = re.compile(r'\binterface\s*=\s*"([^"]+)"')
_XML_GROUP = re.compile(r'\bgroup\s*=\s*"([^"]+)"')
_XML_VERSION = re.compile(r'\bversion\s*=\s*"([^"]+)"')


@dataclass(frozen=True)
class _TypeDecl:
    name: str
    fqn: str
    package: str


@dataclass
class _Interface:
    fqn: str
    package: str
    imports: dict[str, str]
    methods: dict[tuple[str, int], list[tuple[list[str], int]]] = field(
        default_factory=dict
    )


@dataclass
class DubboScanResult:
    deps: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: bool = False


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _package_and_imports(text: str) -> tuple[str, dict[str, str]]:
    package_match = _PACKAGE.search(text)
    package = package_match.group(1) if package_match else ""
    imports: dict[str, str] = {}
    for match in _IMPORT.finditer(text):
        name = match.group(1)
        if name.endswith(".*"):
            imports.setdefault("*", name)
            continue
        simple = name.rsplit(".", 1)[-1]
        imports.setdefault(simple, name)
    return package, imports


def _strip_annotations(value: str) -> str:
    value = re.sub(r"@[A-Za-z_$][\w.$]*(?:\([^)]*\))?\s*", " ", value)
    return re.sub(r"\bfinal\s+", " ", value).strip()


def _rewrite_nested(value: str, classes: dict[str, str], package: str) -> str | None:
    """`Outer.Inner` → `pkg.Outer$Inner`；解析不了返回 None。"""
    parts = value.split(".")
    if len(parts) < 2:
        return None
    head = parts[0]
    base = classes.get(head)
    if not base and package:
        base = classes.get(f"{package}.{head}")
    if not base:
        return None
    return base + "$" + "$".join(parts[1:])


def _resolve_type(
    raw: str,
    *,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
) -> str | None:
    value = _strip_annotations(str(raw or "")).strip()
    if not value:
        return None
    array = ""
    while value.endswith("[]"):
        array += "[]"
        value = value[:-2].strip()
    if value.endswith("..."):
        array += "[]"
        value = value[:-3].strip()
    if not value:
        return None
    value = value.split("<", 1)[0].strip()
    if value in _PRIMITIVES:
        return value + array
    if "." in value:
        if value.startswith("java.") or value.startswith("javax."):
            return value + array
        nested = _rewrite_nested(value, classes, package)
        return (nested or value) + array
    if value in imports:
        return imports[value] + array
    if package and f"{package}.{value}" in classes:
        return f"{package}.{value}" + array
    if value in classes:
        return classes[value] + array
    if value in _JAVA_LANG:
        return f"java.lang.{value}" + array
    return None


def _parameter_types(
    raw: str,
    *,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
) -> list[str] | None:
    if not raw.strip():
        return []
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(raw):
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(raw[start:index])
            start = index + 1
    parts.append(raw[start:])
    result: list[str] = []
    for item in parts:
        cleaned = _strip_annotations(item)
        # 去掉参数名，只保留类型部分。
        match = re.match(r"(.+?)\s+[A-Za-z_$]\w*$", cleaned)
        type_text = match.group(1) if match else cleaned
        resolved = _resolve_type(
            type_text, package=package, imports=imports, classes=classes
        )
        if resolved is None:
            return None
        result.append(resolved)
    return result


def _interface_methods(
    body: str,
    *,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
) -> dict[tuple[str, int], list[tuple[list[str], int]]]:
    methods: dict[tuple[str, int], list[tuple[list[str], int]]] = {}
    for match in _METHOD_DECL.finditer(body):
        name = match.group(2)
        if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
            continue
        params = _parameter_types(
            match.group(3),
            package=package,
            imports=imports,
            classes=classes,
        )
        if params is None:
            continue
        line = _line_of(body, match.start())
        methods.setdefault((name, len(params)), []).append((params, line))
    return methods


def _annotation_args(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(
        r"([A-Za-z_$]\w*)\s*=\s*(?:\"([^\"]*)\"|([\w.$]+)\s*\.\s*class)", raw or ""
    ):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        if value is not None:
            result[key] = value
    return result


def _reference_imports(imports: dict[str, str]) -> bool:
    values = set(imports.values())
    if values & _DUBBO_REFERENCE_IMPORTS:
        return True
    return any(
        item.startswith("org.apache.dubbo.config.annotation")
        or item.startswith("com.alibaba.dubbo.config.annotation")
        for item in values
    )


def _dep_record(
    *,
    service: str,
    method: str,
    parameter_types: list[str],
    group: str,
    version: str,
    call_sites: list[str],
) -> dict[str, Any]:
    dep_id = build_dubbo_dep_id(
        service, method, parameter_types, group=group or None, version=version or None
    )
    return {
        "dep_id": dep_id,
        "dep_type": "dubbo",
        "evidence": "dubbo_reference",
        "method": method,
        "path_key": None,
        "target": None,
        "targets": [],
        "route_host": None,
        "service": service,
        "parameter_types": list(parameter_types),
        "group": group,
        "version": version,
        "call_sites": call_sites,
        "config_keys": [],
        "clients": ["dubbo"],
        "languages": ["java"],
        "inferred": False,
    }


def scan_dubbo_sources(
    *,
    files: list[tuple[Path, str]],
    root: Path,
    read_text: Callable[[Path], str],
    max_files: int = MAX_FILES,
    max_call_sites: int = MAX_CALL_SITES,
) -> DubboScanResult:
    """扫描项目内的 Java/Kotlin 与 Dubbo XML；只读取文本。"""
    result = DubboScanResult()
    sources: list[tuple[str, str]] = []
    classes: dict[str, str] = {}
    interfaces: dict[str, _Interface] = {}
    total = 0
    for path, _language in files:
        if len(sources) >= max_files:
            result.warnings.append("dubbo_scan_truncated_file_count")
            break
        suffix = path.suffix.lower()
        if suffix not in {".java", ".kt", ".kts", ".xml"}:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if total + size > MAX_TOTAL_BYTES:
            result.warnings.append("dubbo_scan_truncated_total_bytes")
            break
        raw = read_text(path)
        if not raw.strip():
            continue
        total += size
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        if suffix in {".kt", ".kts"}:
            if re.search(r"@(?:DubboReference|Reference)\b", raw):
                result.unsupported.append(
                    {
                        "file": rel,
                        "line": _line_of(raw, raw.find("@")),
                        "reason": "dubbo_kotlin_reference_unsupported",
                    }
                )
            continue
        if suffix == ".xml":
            for match in _XML_REFERENCE.finditer(raw):
                interface = _XML_INTERFACE.search(match.group(0))
                result.unsupported.append(
                    {
                        "file": rel,
                        "line": _line_of(raw, match.start()),
                        "reason": "dubbo_xml_reference_unsupported",
                        "detail": interface.group(1) if interface else "",
                    }
                )
            continue
        sources.append((rel, _mask_comments(raw)))
        package, imports = _package_and_imports(raw)
        for match in _TYPE_DECL.finditer(raw):
            name = match.group(1)
            fqn = f"{package}.{name}" if package else name
            classes.setdefault(name, fqn)
            classes.setdefault(fqn, fqn)

    # 接口索引需要先于字段扫描建好，参数类型解析依赖全项目类名。
    for rel, text in sources:
        package, imports = _package_and_imports(text)
        for match in _INTERFACE_DECL.finditer(text):
            name = match.group(1)
            brace = text.find("{", match.start())
            if brace < 0:
                continue
            end = _matching_brace(text, brace)
            fqn = f"{package}.{name}" if package else name
            body = text[brace + 1 : end]
            interfaces[fqn] = _Interface(
                fqn=fqn,
                package=package,
                imports=imports,
                methods=_interface_methods(
                    body,
                    package=package,
                    imports=imports,
                    classes=classes,
                ),
            )

    deps: dict[str, dict[str, Any]] = {}
    for rel, text in sources:
        package, imports = _package_and_imports(text)
        has_dubbo_import = _reference_imports(imports)
        for match in _REFERENCE.finditer(text):
            annotation = match.group("annotation")
            if annotation == "Reference" and not has_dubbo_import:
                continue
            args = _annotation_args(match.group("args") or "")
            field_match = _FIELD.search(_strip_annotations(match.group("decl") or ""))
            if field_match is None:
                result.warnings.append(f"dubbo_reference_unparsed:{rel}")
                continue
            field_type = field_match.group(1).strip()
            field_name = field_match.group(2)
            service = args.get("interfaceName")
            if not service and args.get("interfaceClass"):
                service = _resolve_type(
                    args["interfaceClass"],
                    package=package,
                    imports=imports,
                    classes=classes,
                )
            if not service:
                service = _resolve_type(
                    field_type, package=package, imports=imports, classes=classes
                )
            if not service:
                # 类型名解析不到源码，和“接口只在依赖 jar”是同一个实际后果：
                # 无法确认方法签名，不能生成可绑定的 dep。
                result.unsupported.append(
                    {
                        "file": rel,
                        "line": _line_of(text, match.start()),
                        "reason": "dubbo_interface_in_jar",
                        "detail": field_type,
                    }
                )
                continue
            service = service.split("<", 1)[0]
            iface = interfaces.get(service)
            if iface is None:
                result.unsupported.append(
                    {
                        "file": rel,
                        "line": _line_of(text, match.start()),
                        "reason": "dubbo_interface_in_jar",
                        "detail": service,
                    }
                )
                continue
            group = args.get("group", "")
            version = args.get("version", "")
            group = re.sub(r"^\$\{[^}]*\}$", "", group)
            version = re.sub(r"^\$\{[^}]*\}$", "", version)
            for call in _CALL.finditer(text):
                if call.group(1) != field_name:
                    continue
                method = call.group(2)
                open_paren = text.find("(", call.end() - 1)
                call_args, _end = _call_args(text, open_paren) if open_paren >= 0 else ([], 0)
                candidates = iface.methods.get((method, len(call_args)), [])
                line = _line_of(text, call.start())
                line_start = text.rfind("\n", 0, call.start()) + 1
                call_site_id = f"{rel}:{line}:{call.start() - line_start}"
                if not candidates:
                    result.unsupported.append(
                        {
                            "file": rel,
                            "line": line,
                            "reason": "dubbo_method_unresolved",
                            "detail": f"{service}#{method}/{len(call_args)}",
                            "call_site_id": call_site_id,
                        }
                    )
                    continue
                if len(candidates) > 1:
                    result.unsupported.append(
                        {
                            "file": rel,
                            "line": line,
                            "reason": "dubbo_overload_ambiguous",
                            "detail": f"{service}#{method}/{len(call_args)}",
                            "call_site_id": call_site_id,
                        }
                    )
                    continue
                parameter_types = candidates[0][0]
                try:
                    record = _dep_record(
                        service=service,
                        method=method,
                        parameter_types=parameter_types,
                        group=group,
                        version=version,
                        call_sites=[call_site_id],
                    )
                except DepIdError as exc:
                    result.unsupported.append(
                        {
                            "file": rel,
                            "line": line,
                            "reason": exc.code,
                            "detail": str(exc.detail or ""),
                            "call_site_id": call_site_id,
                        }
                    )
                    continue
                if len(record["dep_id"]) > MAX_DEP_CHARS:
                    result.unsupported.append(
                        {
                            "file": rel,
                            "line": line,
                            "reason": "dubbo_dep_id_too_long",
                            "detail": record["dep_id"][:120],
                        }
                    )
                    continue
                current = deps.get(record["dep_id"])
                if current is None:
                    deps[record["dep_id"]] = record
                else:
                    current["call_sites"] = _dedupe(
                        [*current["call_sites"], *record["call_sites"]]
                    )
                if sum(len(dep["call_sites"]) for dep in deps.values()) >= max_call_sites:
                    result.warnings.append("dubbo_scan_truncated_call_sites")
                    break
    result.deps = sorted(deps.values(), key=lambda item: item["dep_id"])
    result.unsupported.sort(key=lambda item: (item.get("file", ""), item.get("line", 0)))
    result.checked = bool(sources)
    return result


__all__ = ["DubboScanResult", "scan_dubbo_sources"]
