"""Java 源码里的 Dubbo 识别（C3.5.3，不执行被测代码）。

消费端（下游 Mock 用）：

- `@DubboReference` / Dubbo 的 `@Reference` 注解字段；
- 接口源码在同一仓库（`interface` 声明），参数类型能解析到 JVM 名；
- 字段上的方法调用能与接口方法签名唯一匹配。

提供方（被测 Dubbo 入口画像用）：

- `@DubboService`、2.7 的 `org.apache.dubbo...@Service`、XML `<dubbo:service>`；
- 实现类的 `implements` 或 `interfaceName`/`interfaceClass` 能定位接口源码；
- 每个接口方法生成一个 `dubbo:{interface}#{method}({param_types})?...` 端点；
- DTO 字段、必填注解（`@NotNull` 等）、嵌套 DTO 与枚举常量进入
  `parameter_schemas` / `result_schema`，供 A4 与模板采样。

不猜：

- 接口只在依赖 jar、参数/返回类型解析不了、多接口无 override → `unsupported`；
- 2.6 注解、Kotlin 提供方、`ReferenceConfig` 编程式引用显式告警；
- Spring `@Service` 不按简单名误判；
- 动态代理 / 反射 / `getBean` 拿到的引用不在文本扫描范围内。

`dep_id` / `endpoint` 统一由 `astsight.mock.identity.build_dubbo_dep_id`
计算，参数签名用于区分重载。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from ..mock.grpc_descriptor import (
    GrpcDescriptorError,
    GrpcDescriptorIndex,
    GrpcMethod,
)
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
_XML_SERVICE = re.compile(
    r"<dubbo:service\b[^>]*>|<dubbo:service\b[^>]*/>", re.S
)
_XML_INTERFACE = re.compile(r'\binterface\s*=\s*"([^"]+)"')
_XML_REF = re.compile(r'\bref\s*=\s*"([^"]+)"')
_XML_GROUP = re.compile(r'\bgroup\s*=\s*"([^"]+)"')
_XML_VERSION = re.compile(r'\bversion\s*=\s*"([^"]+)"')

# 只认全限定或已导入的 Dubbo 注解；不按简单名猜，避免与 Spring @Service 冲突。
_PROVIDER_ANNOTATION = re.compile(
    r"@(?P<annotation>"
    r"(?:org\.apache\.dubbo\.config\.annotation\.|com\.alibaba\.dubbo\.config\.annotation\.)?"
    r"(?P<name>DubboService|Service))\b"
    r"\s*(?:\((?P<args>[^)]*)\))?",
    re.S,
)
_CLASS_DECL = re.compile(
    r"\bclass\s+([A-Za-z_$][\w$]*)(?P<rest>[^{]*)\{",
    re.S,
)
_IMPLEMENTS = re.compile(r"\bimplements\s+([^{]+)", re.S)
_DUBBO_PROVIDER_IMPORTS = {
    "org.apache.dubbo.config.annotation.DubboService",
    "org.apache.dubbo.config.annotation.Service",
}
_DUBBO_2_6_PROVIDER = "com.alibaba.dubbo.config.annotation.Service"
_DUBBO_ANNOTATION_PACKAGES = (
    "org.apache.dubbo.config.annotation",
    "com.alibaba.dubbo.config.annotation",
)
_KOTLIN_PROVIDER = re.compile(r"@(?:[\w.]*\.)?(?:DubboService)\b")
_KOTLIN_DUBBO_IMPORT = re.compile(
    r"(?m)^\s*import\s+(?:org\.apache|com\.alibaba)\.dubbo\b"
)
_PROPERTY_PORT = re.compile(
    r"(?m)^\s*dubbo\.protocols?\.[A-Za-z0-9_.-]*port\s*=\s*(\d+)\s*$"
)
_PROPERTY_PROTOCOL_NAME = re.compile(
    r"(?m)^\s*dubbo\.protocols?\.[A-Za-z0-9_.-]*name\s*=\s*([A-Za-z0-9_-]+)\s*$"
)
_XML_PROTOCOL = re.compile(r"<dubbo:protocol\b[^>]*>", re.S)
_XML_PORT = re.compile(r'\bport\s*=\s*"(\d+)"')
_XML_NAME = re.compile(r'\bname\s*=\s*"([^"]+)"')
_CLASS_BODY = re.compile(
    r"\b(?P<kind>class|record)\s+([A-Za-z_$][\w$]*)[^{;]*\{"
)
_ENUM_BODY = re.compile(r"\benum\s+([A-Za-z_$][\w$]*)[^{]*\{")
_FIELD_STATEMENT = re.compile(
    r"^\s*(?P<annotations>(?:@[\w.$]+(?:\s*\([^)]*\))?\s*)*)"
    r"(?P<modifiers>(?:(?:public|protected|private|static|final|transient|volatile)\s+)*)"
    r"(?P<type>[A-Za-z_$][\w.$]*(?:\s*<[^;{}]*>)?(?:\s*\[\s*\])*)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:=.*)?$",
    re.S,
)
_RECORD_COMPONENT = re.compile(
    r"^\s*(?P<annotations>(?:@[\w.$]+(?:\s*\([^)]*\))?\s*)*)"
    r"(?P<type>[A-Za-z_$][\w.$]*(?:\s*<[^;{}]*>)?(?:\s*\[\s*\])*)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*$",
    re.S,
)
_REQUIRED_ANNOTATIONS = {
    "NotNull",
    "NotBlank",
    "NotEmpty",
    "NonNull",
    "Required",
}
_CUSTOM_FORMAT_ANNOTATIONS = {
    "Mobile": "phone",
    "Phone": "phone",
    "Telephone": "phone",
    "IdCard": "id_card",
    "IDCard": "id_card",
    "IdNumber": "id_card",
    "Url": "url",
    "URL": "url",
}
_NON_VALIDATION_ANNOTATIONS = {
    "Override",
    "Deprecated",
    "SuppressWarnings",
    "FunctionalInterface",
    "SafeVarargs",
    "Valid",
    "Validated",
    "JsonProperty",
    "JsonIgnore",
    "JsonFormat",
    "JsonAlias",
    "JsonInclude",
    "JsonTypeInfo",
    "JsonSubTypes",
    "JsonDeserialize",
    "JsonSerialize",
    "Schema",
    "ApiModel",
    "ApiModelProperty",
    "ApiOperation",
    "ApiParam",
    "Data",
    "Getter",
    "Setter",
    "Builder",
    "SuperBuilder",
    "NoArgsConstructor",
    "AllArgsConstructor",
    "RequiredArgsConstructor",
    "ToString",
    "EqualsAndHashCode",
    "Slf4j",
    "Log4j2",
    "Value",
    "Component",
    "Service",
    "Repository",
    "Controller",
    "RestController",
    "Configuration",
    "Bean",
    "Autowired",
    "Resource",
    "Qualifier",
    "Transactional",
    "Id",
    "GeneratedValue",
    "Transient",
    "OneToMany",
    "ManyToOne",
    "JoinColumn",
    "Table",
    "Enumerated",
    "Temporal",
    "Lob",
    "Basic",
}
_VALIDATION_HINTS = (
    "Check",
    "Pattern",
    "Regex",
    "Range",
    "Length",
    "Size",
    "NotEmpty",
    "NotBlank",
    "Required",
    "Min",
    "Max",
    "In",
    "Enum",
    "Mobile",
    "Phone",
    "Email",
    "IdCard",
    "Amount",
    "Money",
    "Format",
    "Column",
)
_MAX_SCHEMA_DEPTH = 4
_MAX_SCHEMA_FIELDS = 40


@dataclass(frozen=True)
class _TypeDecl:
    name: str
    fqn: str
    package: str


@dataclass(frozen=True)
class _MethodDecl:
    params: list[str]
    declared_params: list[str]
    parameter_constraints: list[dict[str, Any]]
    line: int
    return_type: str | None
    declared_return_type: str


@dataclass(frozen=True)
class _FieldDecl:
    name: str
    type: str | None
    declared_type: str
    annotations: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    required: bool = False


@dataclass
class _ClassInfo:
    fqn: str
    package: str
    imports: dict[str, str]
    is_enum: bool = False
    fields: list[_FieldDecl] = field(default_factory=list)
    enum_values: list[str] = field(default_factory=list)


@dataclass
class _Interface:
    fqn: str
    package: str
    imports: dict[str, str]
    methods: dict[tuple[str, int], list[_MethodDecl]] = field(
        default_factory=dict
    )


@dataclass
class _TripleContract:
    source: str
    index: GrpcDescriptorIndex
    methods: dict[str, GrpcMethod]


@dataclass
class DubboScanResult:
    deps: list[dict[str, Any]] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)
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


def _mask_annotation_args(text: str) -> str:
    """把 `@Foo(...)` 的参数替换成等长空格，供方法签名正则匹配。

    `_METHOD_DECL` 的 `([^)]*)` 遇到注解参数里的括号会提前结束；这里只屏蔽
    参数、保留注解名与原文偏移，约束仍从原文本解析。
    """
    chars = list(text)
    index = 0
    length = len(text)
    while index < length:
        at = text.find("@", index)
        if at < 0:
            break
        match = re.match(r"@[\w.$]+", text[at:])
        if match is None:
            index = at + 1
            continue
        cursor = at + len(match.group(0))
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length or text[cursor] != "(":
            index = cursor
            continue
        depth = 0
        start = cursor
        in_string = False
        escaped = False
        while cursor < length:
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        for position in range(start, cursor + 1):
                            if chars[position] not in "\r\n":
                                chars[position] = " "
                        index = cursor + 1
                        break
            cursor += 1
        else:
            break
    return "".join(chars)


def _parameter_details(
    raw: str,
    *,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
) -> list[dict[str, Any]] | None:
    if not raw.strip():
        return []
    result: list[dict[str, Any]] = []
    for item in _split_type_list(raw):
        details = _annotation_details(item)
        cleaned = _strip_annotations(item)
        # 去掉参数名，只保留类型部分。
        match = re.match(r"(.+?)\s+[A-Za-z_$]\w*$", cleaned)
        type_text = (match.group(1) if match else cleaned).strip()
        resolved = _resolve_type(
            type_text, package=package, imports=imports, classes=classes
        )
        if resolved is None:
            return None
        result.append(
            {
                "type": resolved,
                "declared": type_text,
                "constraints": _constraints_from_annotations(details),
            }
        )
    return result


def _interface_methods(
    body: str,
    *,
    text: str,
    body_start: int,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
) -> dict[tuple[str, int], list[_MethodDecl]]:
    methods: dict[tuple[str, int], list[_MethodDecl]] = {}
    masked = _mask_annotation_args(body)
    for match in _METHOD_DECL.finditer(masked):
        name = match.group(2)
        if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
            continue
        # 正则跑在屏蔽注解参数的副本上；参数原文按同一偏移取回，保留约束。
        raw_params = body[match.start(3) : match.end(3)]
        details = _parameter_details(
            raw_params,
            package=package,
            imports=imports,
            classes=classes,
        )
        if details is None:
            continue
        params = [item["type"] for item in details]
        declared_params = [item["declared"] for item in details]
        parameter_constraints = [item["constraints"] for item in details]
        declared_return = _strip_annotations(match.group(1)).strip()
        return_type = _resolve_type(
            declared_return,
            package=package,
            imports=imports,
            classes=classes,
        )
        line = _line_of(text, body_start + match.start())
        methods.setdefault((name, len(params)), []).append(
            _MethodDecl(
                params=params,
                declared_params=declared_params,
                parameter_constraints=parameter_constraints,
                line=line,
                return_type=return_type,
                declared_return_type=declared_return,
            )
        )
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


def _split_type_list(raw: str) -> list[str]:
    """按顶层逗号切分 `implements A, B<C>`；忽略泛型、括号与花括号内的逗号。"""
    parts: list[str] = []
    depth = 0
    start = 0
    in_string = False
    escaped = False
    for index, char in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "<({":
            depth += 1
        elif char in ">)}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(raw[start:index])
            start = index + 1
    parts.append(raw[start:])
    return [part.strip() for part in parts if part.strip()]


def _clean_group_version(value: str) -> str:
    """`${dubbo.group}` 这类占位符无法静态求值，按未设置处理。"""
    return re.sub(r"^\$\{[^}]*\}$", "", value or "")


def _dubbo_protocol_ports(raw: str, suffix: str) -> set[int]:
    """从 properties / yaml / XML 里读取 Dubbo 协议端口；读不到不猜。"""
    ports: set[int] = set()
    if suffix == ".properties":
        for match in _PROPERTY_PORT.finditer(raw):
            ports.add(int(match.group(1)))
        return ports
    if suffix in {".yml", ".yaml"}:
        try:
            payload = yaml.safe_load(raw)
        except yaml.YAMLError:
            return ports
        if not isinstance(payload, dict):
            return ports
        dubbo = payload.get("dubbo")
        if not isinstance(dubbo, dict):
            return ports
        protocol = dubbo.get("protocol")
        if isinstance(protocol, dict) and protocol.get("port") is not None:
            try:
                ports.add(int(protocol["port"]))
            except (TypeError, ValueError):
                pass
        protocols = dubbo.get("protocols")
        if isinstance(protocols, dict):
            for value in protocols.values():
                if not isinstance(value, dict) or value.get("port") is None:
                    continue
                try:
                    ports.add(int(value["port"]))
                except (TypeError, ValueError):
                    pass
        return ports
    if suffix == ".xml":
        for match in _XML_PROTOCOL.finditer(raw):
            port = _XML_PORT.search(match.group(0))
            if port is not None:
                ports.add(int(port.group(1)))
    return ports


def _dubbo_protocol_names(raw: str, suffix: str) -> set[str]:
    names: set[str] = set()
    if suffix == ".properties":
        names.update(
            match.group(1).strip().lower()
            for match in _PROPERTY_PROTOCOL_NAME.finditer(raw)
        )
        return names
    if suffix in {".yml", ".yaml"}:
        try:
            payload = yaml.safe_load(raw)
        except yaml.YAMLError:
            return names
        if not isinstance(payload, dict):
            return names
        dubbo = payload.get("dubbo")
        if not isinstance(dubbo, dict):
            return names
        protocol = dubbo.get("protocol")
        if isinstance(protocol, dict) and protocol.get("name"):
            names.add(str(protocol["name"]).strip().lower())
        protocols = dubbo.get("protocols")
        if isinstance(protocols, dict):
            for value in protocols.values():
                if isinstance(value, dict) and value.get("name"):
                    names.add(str(value["name"]).strip().lower())
        return names
    if suffix == ".xml":
        for match in _XML_PROTOCOL.finditer(raw):
            name = _XML_NAME.search(match.group(0))
            if name is not None:
                names.add(name.group(1).strip().lower())
    return names


def _triple_contracts(
    proto_paths: list[Path],
    *,
    root: Path,
) -> tuple[dict[str, _TripleContract], list[str]]:
    contracts: dict[str, _TripleContract] = {}
    warnings: list[str] = []
    include_dirs = sorted({path.parent for path in proto_paths})
    for path in proto_paths:
        try:
            index = GrpcDescriptorIndex.load(path, include_dirs=include_dirs)
        except GrpcDescriptorError as exc:
            warnings.append(
                f"dubbo_triple_descriptor_invalid:{path.relative_to(root).as_posix()}:"
                f"{exc.code}"
            )
            continue
        source = path.relative_to(root).as_posix()
        services: dict[str, dict[str, GrpcMethod]] = {}
        for method in index.unary_methods():
            services.setdefault(method.service, {})[method.method] = method
        for service, methods in services.items():
            current = contracts.get(service)
            if current is None:
                contracts[service] = _TripleContract(
                    source=source,
                    index=index,
                    methods=methods,
                )
            else:
                current.methods.update(methods)
    return contracts, warnings


def _matches_triple_contract(
    service: str,
    contracts: dict[str, _TripleContract],
) -> str | None:
    if service in contracts:
        return service
    short = service.rsplit(".", 1)[-1]
    matches = [
        name for name in contracts if name.rsplit(".", 1)[-1] == short
    ]
    return matches[0] if len(matches) == 1 else None


def _annotation_mentions_protocol(raw: str, protocol: str) -> bool:
    return bool(
        re.search(
            rf"\bprotocol\s*=\s*\{{[^}}]*[\"']{re.escape(protocol)}[\"']",
            raw or "",
            re.S,
        )
    )


_SCALAR_JVM_TYPES = {
    "java.lang.String",
    "java.lang.CharSequence",
    "java.util.UUID",
    "java.net.URI",
    "java.net.URL",
    "int",
    "java.lang.Integer",
    "short",
    "java.lang.Short",
    "byte",
    "java.lang.Byte",
    "long",
    "java.lang.Long",
    "float",
    "java.lang.Float",
    "double",
    "java.lang.Double",
    "boolean",
    "java.lang.Boolean",
    "char",
    "java.lang.Character",
    "java.math.BigDecimal",
    "java.math.BigInteger",
}
_COLLECTION_TYPES = {
    "java.util.List",
    "java.util.Set",
    "java.util.Collection",
    "java.util.ArrayList",
    "java.util.LinkedList",
    "java.util.HashSet",
    "java.util.LinkedHashSet",
}
_MAP_TYPES = {
    "java.util.Map",
    "java.util.HashMap",
    "java.util.LinkedHashMap",
    "java.util.TreeMap",
}
_TEMPORAL_TYPES = {
    "java.util.Date": "datetime",
    "java.sql.Date": "date",
    "java.sql.Time": "time",
    "java.sql.Timestamp": "datetime",
    "java.time.LocalDate": "date",
    "java.time.LocalTime": "time",
    "java.time.LocalDateTime": "datetime",
    "java.time.OffsetDateTime": "datetime",
    "java.time.ZonedDateTime": "datetime",
    "java.time.Instant": "instant",
    "java.time.Duration": "duration",
    "java.time.Period": "period",
}


def _top_level_statements(body: str):
    """按顶层 `;` 切出字段区间；跳过方法体/内部类等 `{...}` 块。"""
    start = 0
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char in "\"'":
            quote = char
            index += 1
            while index < length and body[index] != quote:
                if body[index] == "\\":
                    index += 1
                index += 1
            index += 1
            continue
        if char == "{":
            end = _matching_brace(body, index)
            if end <= index:
                return
            index = end + 1
            start = index
            continue
        if char == ";":
            yield start, index
            start = index + 1
        index += 1


def _parse_literal(text: str) -> Any:
    value = (text or "").strip()
    if value.startswith("{") and value.endswith("}"):
        return [
            _parse_literal(item)
            for item in _split_type_list(value[1:-1])
        ]
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if value in {"true", "false"}:
        return value == "true"
    if value.endswith(".class"):
        return value[: -len(".class")]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _annotation_args(raw: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    positional: list[str] = []
    for part in _split_type_list(raw or ""):
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip()
            if re.fullmatch(r"[A-Za-z_$]\w*", key):
                values[key] = _parse_literal(value)
                continue
        positional.append(part)
    if "value" not in values and positional:
        values["value"] = _parse_literal(positional[0])
    return values


def _annotation_details(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """解析注解名与参数；括号内的字符串/嵌套括号不截断。"""
    details: list[tuple[str, dict[str, Any]]] = []
    index = 0
    length = len(raw or "")
    while index < length:
        at = raw.find("@", index)
        if at < 0:
            break
        match = re.match(r"@([\w.$]+)", raw[at:])
        if match is None:
            index = at + 1
            continue
        name = match.group(1)
        end = at + len(match.group(0))
        while end < length and raw[end].isspace():
            end += 1
        args = ""
        if end < length and raw[end] == "(":
            depth = 0
            start = end + 1
            cursor = end
            in_string = False
            escaped = False
            while cursor < length:
                char = raw[cursor]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                else:
                    if char == '"':
                        in_string = True
                    elif char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                        if depth == 0:
                            args = raw[start:cursor]
                            end = cursor + 1
                            break
                cursor += 1
        details.append((name, _annotation_args(args)))
        index = max(end, at + 1)
    return details


def _as_number(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return value


def _looks_like_custom_validation(short: str) -> bool:
    if short in _NON_VALIDATION_ANNOTATIONS:
        return False
    if short in _CUSTOM_FORMAT_ANNOTATIONS:
        return True
    return any(hint in short for hint in _VALIDATION_HINTS)


def _apply_custom_constraints(
    short: str,
    args: dict[str, Any],
    constraints: dict[str, Any],
) -> None:
    constraints.setdefault("custom", {})[short] = dict(args)
    if args.get("required") is True or args.get("nullable") is False:
        constraints["required"] = True
    if "length" in args:
        constraints.setdefault("max_length", _as_number(args["length"]))
    for key in ("minLength", "min_length", "minLen", "minSize"):
        if key in args:
            constraints.setdefault("min_length", _as_number(args[key]))
    for key in ("maxLength", "max_length", "maxLen", "maxSize"):
        if key in args:
            constraints.setdefault("max_length", _as_number(args[key]))
    length_named = any(token in short for token in ("Length", "Size", "Len"))
    if "min" in args:
        constraints.setdefault(
            "min_length" if length_named else "minimum",
            _as_number(args["min"]),
        )
    if "max" in args:
        constraints.setdefault(
            "max_length" if length_named else "maximum",
            _as_number(args["max"]),
        )
    for key in ("minValue", "min_value"):
        if key in args:
            constraints.setdefault("minimum", _as_number(args[key]))
    for key in ("maxValue", "max_value"):
        if key in args:
            constraints.setdefault("maximum", _as_number(args[key]))
    for key in ("pattern", "regexp", "regex"):
        if args.get(key):
            constraints.setdefault("pattern", str(args[key]))
    if args.get("format"):
        constraints.setdefault("format", str(args["format"]).lower())
    for key in ("values", "options", "enumValues", "allowedValues"):
        value = args.get(key)
        if isinstance(value, list) and value:
            constraints.setdefault("allowed_values", value)
    if short in _CUSTOM_FORMAT_ANNOTATIONS:
        constraints.setdefault("format", _CUSTOM_FORMAT_ANNOTATIONS[short])


def _constraints_from_annotations(
    details: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    for name, args in details:
        short = name.rsplit(".", 1)[-1]
        if short in _REQUIRED_ANNOTATIONS:
            constraints["required"] = True
        elif short in {"Size", "Length"}:
            if "min" in args:
                constraints["min_length"] = _as_number(args["min"])
            if "max" in args:
                constraints["max_length"] = _as_number(args["max"])
        elif short == "Min" and "value" in args:
            constraints["minimum"] = _as_number(args["value"])
        elif short == "Max" and "value" in args:
            constraints["maximum"] = _as_number(args["value"])
        elif short in {"DecimalMin", "DecimalMax"} and "value" in args:
            key = "minimum" if short == "DecimalMin" else "maximum"
            constraints[key] = _as_number(args["value"])
            if args.get("inclusive") is False:
                constraints[f"{key}_exclusive"] = True
        elif short == "Pattern":
            pattern = args.get("regexp", args.get("value"))
            if pattern:
                constraints["pattern"] = str(pattern)
        elif short == "Email":
            constraints["format"] = "email"
        elif short in {"Positive", "PositiveOrZero"}:
            constraints["minimum"] = 0
            if short == "Positive":
                constraints["minimum_exclusive"] = True
        elif short in {"Negative", "NegativeOrZero"}:
            constraints["maximum"] = 0
            if short == "Negative":
                constraints["maximum_exclusive"] = True
        elif short in {"Past", "Future"}:
            constraints["temporal"] = short.lower()
        elif _looks_like_custom_validation(short):
            _apply_custom_constraints(short, args, constraints)
    return constraints


def _is_required_field(
    annotations: tuple[str, ...],
    constraints: dict[str, Any] | None = None,
) -> bool:
    if constraints and constraints.get("required"):
        return True
    return any(name.rsplit(".", 1)[-1] in _REQUIRED_ANNOTATIONS for name in annotations)


def _parse_fields(
    body: str,
    *,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
) -> list[_FieldDecl]:
    fields: list[_FieldDecl] = []
    # 先在屏蔽注解参数的副本上切结构，避免注解里的 {} 被当成代码块；
    # 约束仍从原文同一区间解析。
    masked = _mask_annotation_args(body)
    for start, end in _top_level_statements(masked):
        statement = masked[start:end]
        raw_statement = body[start:end]
        match = _FIELD_STATEMENT.match(statement)
        if match is None:
            continue
        modifiers = (match.group("modifiers") or "").split()
        if "static" in modifiers:
            continue
        declared = _strip_annotations(match.group("type")).strip()
        resolved = _resolve_type(
            declared, package=package, imports=imports, classes=classes
        )
        details = _annotation_details(raw_statement)
        annotations = tuple(name for name, _args in details)
        constraints = _constraints_from_annotations(details)
        fields.append(
            _FieldDecl(
                name=match.group("name"),
                type=resolved,
                declared_type=declared,
                annotations=annotations,
                constraints=constraints,
                required=_is_required_field(annotations, constraints),
            )
        )
    return fields


def _record_header(text: str, start: int) -> str:
    open_index = text.find("(", start)
    if open_index < 0:
        return ""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    return ""


def _parse_record_components(
    text: str,
    match: re.Match,
    *,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
) -> list[_FieldDecl]:
    fields: list[_FieldDecl] = []
    for item in _split_type_list(_record_header(text, match.start())):
        component = _RECORD_COMPONENT.match(item)
        if component is None:
            continue
        declared = component.group("type").strip()
        resolved = _resolve_type(
            declared, package=package, imports=imports, classes=classes
        )
        details = _annotation_details(component.group("annotations") or "")
        annotations = tuple(name for name, _args in details)
        constraints = _constraints_from_annotations(details)
        fields.append(
            _FieldDecl(
                name=component.group("name"),
                type=resolved,
                declared_type=declared,
                annotations=annotations,
                constraints=constraints,
                required=True,
            )
        )
    return fields


def _parse_enum_constants(body: str) -> list[str]:
    head = body.split(";", 1)[0]
    names = re.findall(
        r"(?:^|,)\s*(?:@[\w.$]+(?:\s*\([^)]*\))?\s*)*([A-Za-z_$][\w$]*)",
        head,
    )
    return [name for name in names if name not in {"public", "private", "protected"}]


def _generic_arguments(declared: str) -> list[str]:
    match = re.search(r"<(.*)>\s*$", declared or "", re.S)
    if match is None:
        return []
    return _split_type_list(match.group(1))


def _schema_from_declared(
    declared: str,
    class_index: dict[str, _ClassInfo],
    *,
    package: str,
    imports: dict[str, str],
    classes: dict[str, str],
    depth: int,
    seen: set[str] | None,
) -> dict[str, Any] | None:
    text = re.sub(
        r"^\?\s*(?:extends|super)\s+", "", (declared or "").strip()
    )
    if not text:
        return None
    resolved = _resolve_type(
        text, package=package, imports=imports, classes=classes
    )
    return _schema_for_type(
        resolved,
        class_index,
        declared_type=text,
        package=package,
        imports=imports,
        classes=classes,
        depth=depth,
        seen=seen,
    )


def _schema_for_type(
    type_name: str | None,
    class_index: dict[str, _ClassInfo],
    *,
    declared_type: str = "",
    package: str = "",
    imports: dict[str, str] | None = None,
    classes: dict[str, str] | None = None,
    depth: int = 0,
    seen: set[str] | None = None,
) -> dict[str, Any]:
    imports = imports or {}
    classes = classes or {}
    if not type_name:
        return {"kind": "unknown", "type": ""}
    if type_name in _TEMPORAL_TYPES:
        return {
            "kind": "temporal",
            "type": type_name,
            "format": _TEMPORAL_TYPES[type_name],
        }
    if type_name in _SCALAR_JVM_TYPES:
        return {"kind": "scalar", "type": type_name}
    if type_name.endswith("[]"):
        element = _schema_from_declared(
            (declared_type or type_name)[:-2],
            class_index,
            package=package,
            imports=imports,
            classes=classes,
            depth=depth + 1,
            seen=seen,
        )
        return {"kind": "array", "type": type_name, "element": element}
    if type_name in _COLLECTION_TYPES:
        arguments = _generic_arguments(declared_type)
        element = (
            _schema_from_declared(
                arguments[0],
                class_index,
                package=package,
                imports=imports,
                classes=classes,
                depth=depth + 1,
                seen=seen,
            )
            if arguments
            else None
        )
        return {"kind": "collection", "type": type_name, "element": element}
    if type_name in _MAP_TYPES:
        arguments = _generic_arguments(declared_type)
        key = (
            _schema_from_declared(
                arguments[0],
                class_index,
                package=package,
                imports=imports,
                classes=classes,
                depth=depth + 1,
                seen=seen,
            )
            if len(arguments) >= 1
            else None
        )
        value = (
            _schema_from_declared(
                arguments[1],
                class_index,
                package=package,
                imports=imports,
                classes=classes,
                depth=depth + 1,
                seen=seen,
            )
            if len(arguments) >= 2
            else None
        )
        return {"kind": "map", "type": type_name, "key": key, "value": value}
    info = class_index.get(type_name)
    if info is None:
        # 已知但不是仓库源码里的类型（第三方 jar/注解等）：用空对象采样，
        # 并在用例 note 里标明需要人工确认。
        return {"kind": "opaque", "type": type_name}
    if info.is_enum:
        return {
            "kind": "enum",
            "type": type_name,
            "enum_values": list(info.enum_values),
        }
    seen = set(seen or ())
    if type_name in seen or depth >= _MAX_SCHEMA_DEPTH:
        return {"kind": "reference", "type": type_name}
    seen.add(type_name)
    fields: list[dict[str, Any]] = []
    for field in info.fields[:_MAX_SCHEMA_FIELDS]:
        # 每个字段独立记录路径，允许不同分支引用同一 DTO，同时仍能断环。
        schema = _schema_for_type(
            field.type,
            class_index,
            declared_type=field.declared_type,
            package=info.package,
            imports=info.imports,
            classes=classes,
            depth=depth + 1,
            seen=set(seen),
        )
        item: dict[str, Any] = {
            "name": field.name,
            "type": field.type,
            "declared_type": field.declared_type,
            "required": field.required,
            "annotations": list(field.annotations),
            "schema": schema,
        }
        if field.constraints:
            item["constraints"] = dict(field.constraints)
        fields.append(item)
    return {"kind": "dto", "type": type_name, "fields": fields}


def _provider_kind(
    simple: str,
    qualified: str | None,
    imports: dict[str, str],
) -> tuple[str | None, bool]:
    """返回 (注解全限定名, 是否 2.6)；无法确认属于 Dubbo 时返回 (None, False)。"""
    if qualified:
        fqn: str | None = qualified
    else:
        fqn = imports.get(simple)
        if fqn is None:
            wildcard = imports.get("*") or ""
            for package in _DUBBO_ANNOTATION_PACKAGES:
                if wildcard.startswith(package + "."):
                    fqn = f"{package}.{simple}"
                    break
    if fqn == _DUBBO_2_6_PROVIDER:
        return fqn, True
    if fqn in _DUBBO_PROVIDER_IMPORTS:
        return fqn, False
    return None, False


def _provider_records(
    *,
    service: str,
    impl: str,
    iface: _Interface,
    class_index: dict[str, _ClassInfo],
    classes: dict[str, str],
    group: str,
    version: str,
    direct_url: str | None,
    protocol_port: int | None,
    file: str,
    line: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for (method_name, _count), declarations in iface.methods.items():
        for declaration in declarations:
            try:
                endpoint = build_dubbo_dep_id(
                    service,
                    method_name,
                    declaration.params,
                    group=group or None,
                    version=version or None,
                )
            except DepIdError:
                continue
            records.append(
                {
                    "endpoint": endpoint,
                    "transport": "dubbo",
                    "service": service,
                    "method": method_name,
                    "parameter_types": list(declaration.params),
                    "declared_parameter_types": list(declaration.declared_params),
                    "return_type": declaration.return_type,
                    "return_type_declared": declaration.declared_return_type,
                    "parameter_schemas": _parameter_schemas(
                        declaration, iface, class_index, classes
                    ),
                    "result_schema": (
                        _schema_for_type(
                            declaration.return_type,
                            class_index,
                            declared_type=declaration.declared_return_type,
                            package=iface.package,
                            imports=iface.imports,
                            classes=classes,
                        )
                        if declaration.return_type
                        else None
                    ),
                    "group": group,
                    "version": version,
                    "direct_url": direct_url,
                    "protocol_port": protocol_port,
                    "url_source": (
                        "annotation"
                        if direct_url
                        else ("protocol_port" if protocol_port else "unresolved")
                    ),
                    "impl_class": impl,
                    "business_key": (
                        f"{impl}#{method_name}" if impl else f"{service}#{method_name}"
                    ),
                    "handler": method_name,
                    "file": file,
                    "line": line,
                    "method_line": declaration.line,
                    "confidence": "verified",
                    "contract_incomplete": declaration.return_type is None,
                    "contract_incomplete_reason": (
                        "dubbo_return_type_unresolved"
                        if declaration.return_type is None
                        else None
                    ),
                    "source": "dubbo_service",
                }
            )
    return records


def _triple_provider_records(
    *,
    service: str,
    contract: _TripleContract,
    impl: str,
    group: str,
    version: str,
    direct_url: str | None,
    protocol_port: int | None,
    file: str,
    line: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for method in sorted(contract.methods.values(), key=lambda item: item.method):
        try:
            endpoint = build_dubbo_dep_id(
                service,
                method.method,
                [method.request_type],
                group=group or None,
                version=version or None,
            )
        except DepIdError:
            continue
        records.append(
            {
                "endpoint": endpoint,
                "transport": "dubbo",
                "protocol": "tri",
                "service": service,
                "method": method.method,
                "parameter_types": [method.request_type],
                "declared_parameter_types": [method.request_type],
                "return_type": method.response_type,
                "return_type_declared": method.response_type,
                "parameter_schemas": [
                    contract.index.sample_message(method.request_type)
                ],
                "result_schema": contract.index.sample_message(
                    method.response_type
                ),
                "group": group,
                "version": version,
                "direct_url": direct_url,
                "protocol_port": protocol_port,
                "url_source": (
                    "annotation"
                    if direct_url
                    else ("protocol_port" if protocol_port else "unresolved")
                ),
                "impl_class": impl,
                "business_key": f"{impl}#{method.method}",
                "handler": method.method,
                "file": file,
                "line": line,
                "method_line": line,
                "confidence": "verified",
                "contract_incomplete": False,
                "contract_incomplete_reason": None,
                "source": "dubbo_triple_service",
                "serialization": "protobuf",
                "descriptor_source": contract.source,
                "grpc_path": method.grpc_path,
                "compatibility": "grpc_compatible",
            }
        )
    return records


def _parameter_schemas(
    declaration: _MethodDecl,
    iface: _Interface,
    class_index: dict[str, _ClassInfo],
    classes: dict[str, str],
) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for index, type_name in enumerate(declaration.params):
        declared = (
            declaration.declared_params[index]
            if index < len(declaration.declared_params)
            else ""
        )
        schema = _schema_for_type(
            type_name,
            class_index,
            declared_type=declared,
            package=iface.package,
            imports=iface.imports,
            classes=classes,
        )
        constraints = (
            declaration.parameter_constraints[index]
            if index < len(declaration.parameter_constraints)
            else {}
        )
        if constraints:
            schema["constraints"] = dict(constraints)
        schemas.append(schema)
    return schemas


def _add_provider(
    providers: dict[str, dict[str, Any]],
    record: dict[str, Any],
    result: DubboScanResult,
) -> None:
    endpoint = record["endpoint"]
    if endpoint in providers:
        result.warnings.append(f"dubbo_provider_duplicate:{endpoint}")
        return
    providers[endpoint] = record


def _scan_provider_annotations(
    *,
    rel: str,
    text: str,
    interfaces: dict[str, _Interface],
    classes: dict[str, str],
    class_index: dict[str, _ClassInfo],
    providers: dict[str, dict[str, Any]],
    protocol_port: int | None,
    protocols: set[str],
    triple_contracts: dict[str, _TripleContract],
    result: DubboScanResult,
) -> None:
    package, imports = _package_and_imports(text)
    for match in _PROVIDER_ANNOTATION.finditer(text):
        annotation = match.group("annotation")
        simple = match.group("name")
        qualified = annotation if "." in annotation else None
        fqn, is_2_6 = _provider_kind(simple, qualified, imports)
        if fqn is None:
            continue
        line = _line_of(text, match.start())
        if is_2_6:
            result.unsupported.append(
                {
                    "file": rel,
                    "line": line,
                    "reason": "dubbo_2_6_unsupported",
                    "kind": "provider",
                    "detail": fqn,
                }
            )
            continue

        # 注解必须直接落在 class 上；字段/方法上的同名注解不算提供方。
        window = text[match.end() : match.end() + 4_000]
        class_match = _CLASS_DECL.search(window)
        if class_match is None:
            result.warnings.append(f"dubbo_provider_unparsed:{rel}")
            continue
        between = window[: class_match.start()]
        if ";" in between or "{" in between:
            continue

        impl_name = class_match.group(1)
        impl_fqn = f"{package}.{impl_name}" if package else impl_name
        impl_interfaces: list[str] = []
        unresolved_interfaces: list[str] = []
        implements = _IMPLEMENTS.search(class_match.group("rest") or "")
        if implements is not None:
            for item in _split_type_list(implements.group(1)):
                resolved = _resolve_type(
                    item, package=package, imports=imports, classes=classes
                )
                if resolved:
                    impl_interfaces.append(resolved)
                else:
                    unresolved_interfaces.append(item)
        annotation_raw = match.group("args") or ""
        args = _annotation_args(annotation_raw)
        group = _clean_group_version(args.get("group", ""))
        version = _clean_group_version(args.get("version", ""))
        direct_url = args.get("url") or None
        triple_enabled = (
            protocols == {"tri"}
            or str(direct_url or "").startswith("tri://")
            or _annotation_mentions_protocol(annotation_raw, "tri")
        )
        explicit = None
        if args.get("interfaceClass"):
            explicit = _resolve_type(
                args["interfaceClass"],
                package=package,
                imports=imports,
                classes=classes,
            )
        elif args.get("interfaceName"):
            explicit = args["interfaceName"]
        triple_service = None
        if triple_enabled:
            candidates = [
                explicit or args.get("interfaceClass") or args.get("interfaceName"),
                *impl_interfaces,
                *unresolved_interfaces,
            ]
            for candidate in candidates:
                if not candidate:
                    continue
                matched = _matches_triple_contract(str(candidate), triple_contracts)
                if matched:
                    triple_service = matched
                    break

        if triple_service:
            service = triple_service
        else:
            if explicit:
                service = explicit.split("<", 1)[0]
            elif len(impl_interfaces) == 1:
                service = impl_interfaces[0]
            elif not impl_interfaces:
                if unresolved_interfaces:
                    # 实现了接口但源码不在仓库：与消费端一样标
                    # `interface_in_jar`，不把“解析不了”说成“没实现接口”。
                    result.unsupported.append(
                        {
                            "file": rel,
                            "line": line,
                            "reason": "dubbo_interface_in_jar",
                            "kind": "provider",
                            "detail": ",".join(unresolved_interfaces),
                        }
                    )
                else:
                    result.unsupported.append(
                        {
                            "file": rel,
                            "line": line,
                            "reason": "dubbo_provider_interface_missing",
                            "kind": "provider",
                            "detail": impl_fqn,
                        }
                    )
                continue
            else:
                result.unsupported.append(
                    {
                        "file": rel,
                        "line": line,
                        "reason": "dubbo_provider_ambiguous",
                        "kind": "provider",
                        "detail": ",".join(impl_interfaces),
                    }
                )
                continue

        iface = interfaces.get(service)
        if iface is None and not triple_service:
            result.unsupported.append(
                {
                    "file": rel,
                    "line": line,
                    "reason": "dubbo_interface_in_jar",
                    "kind": "provider",
                    "detail": service,
                }
            )
            continue

        if triple_service and triple_enabled:
            records = _triple_provider_records(
                service=triple_service,
                contract=triple_contracts[triple_service],
                impl=impl_fqn,
                group=group,
                version=version,
                direct_url=direct_url,
                protocol_port=protocol_port,
                file=rel,
                line=line,
            )
        else:
            assert iface is not None
            records = _provider_records(
                service=service,
                impl=impl_fqn,
                iface=iface,
                class_index=class_index,
                classes=classes,
                group=group,
                version=version,
                direct_url=direct_url,
                protocol_port=protocol_port,
                file=rel,
                line=line,
            )
        for record in records:
            _add_provider(providers, record, result)


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
    class_index: dict[str, _ClassInfo] = {}
    providers: dict[str, dict[str, Any]] = {}
    xml_services: list[dict[str, Any]] = []
    protocol_ports: set[int] = set()
    protocol_names: set[str] = set()
    proto_paths: list[Path] = []
    total = 0
    for path, _language in files:
        if len(sources) >= max_files:
            result.warnings.append("dubbo_scan_truncated_file_count")
            break
        suffix = path.suffix.lower()
        if suffix not in {
            ".java", ".kt", ".kts", ".xml", ".properties", ".yml", ".yaml",
            ".proto",
        }:
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
        if suffix == ".proto":
            proto_paths.append(path)
            continue
        if suffix in {".kt", ".kts"}:
            if re.search(r"@(?:DubboReference|Reference)\b", raw):
                result.unsupported.append(
                    {
                        "file": rel,
                        "line": _line_of(raw, raw.find("@")),
                        "reason": "dubbo_kotlin_reference_unsupported",
                    }
                )
            if _KOTLIN_PROVIDER.search(raw) or (
                _KOTLIN_DUBBO_IMPORT.search(raw) and re.search(r"@Service\b", raw)
            ):
                result.unsupported.append(
                    {
                        "file": rel,
                        "line": _line_of(raw, raw.find("@")),
                        "reason": "dubbo_kotlin_provider_unsupported",
                        "kind": "provider",
                    }
                )
            continue
        if suffix in {".properties", ".yml", ".yaml"}:
            protocol_ports.update(_dubbo_protocol_ports(raw, suffix))
            protocol_names.update(_dubbo_protocol_names(raw, suffix))
            continue
        if suffix == ".xml":
            protocol_ports.update(_dubbo_protocol_ports(raw, suffix))
            protocol_names.update(_dubbo_protocol_names(raw, suffix))
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
            for match in _XML_SERVICE.finditer(raw):
                interface = _XML_INTERFACE.search(match.group(0))
                if interface is None:
                    result.unsupported.append(
                        {
                            "file": rel,
                            "line": _line_of(raw, match.start()),
                            "reason": "dubbo_xml_service_interface_missing",
                            "kind": "provider",
                        }
                    )
                    continue
                ref = _XML_REF.search(match.group(0))
                group = _XML_GROUP.search(match.group(0))
                version = _XML_VERSION.search(match.group(0))
                xml_services.append(
                    {
                        "file": rel,
                        "line": _line_of(raw, match.start()),
                        "service": interface.group(1),
                        "ref": ref.group(1) if ref else "",
                        "group": group.group(1) if group else "",
                        "version": version.group(1) if version else "",
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
                    text=text,
                    body_start=brace + 1,
                    package=package,
                    imports=imports,
                    classes=classes,
                ),
            )

    # DTO 字段与枚举常量：供模板采样和 A4 生成 arguments。
    for rel, text in sources:
        package, imports = _package_and_imports(text)
        for match in _CLASS_BODY.finditer(text):
            name = match.group(2)
            brace = text.find("{", match.start())
            if brace < 0:
                continue
            end = _matching_brace(text, brace)
            if end <= brace:
                continue
            fqn = f"{package}.{name}" if package else name
            body = text[brace + 1 : end]
            fields = (
                _parse_record_components(
                    text,
                    match,
                    package=package,
                    imports=imports,
                    classes=classes,
                )
                if match.group("kind") == "record"
                else _parse_fields(
                    body, package=package, imports=imports, classes=classes
                )
            )
            class_index[fqn] = _ClassInfo(
                fqn=fqn,
                package=package,
                imports=imports,
                fields=fields,
            )
        for match in _ENUM_BODY.finditer(text):
            name = match.group(1)
            brace = text.find("{", match.start())
            if brace < 0:
                continue
            end = _matching_brace(text, brace)
            if end <= brace:
                continue
            fqn = f"{package}.{name}" if package else name
            class_index[fqn] = _ClassInfo(
                fqn=fqn,
                package=package,
                imports=imports,
                is_enum=True,
                enum_values=_parse_enum_constants(text[brace + 1 : end]),
            )

    protocol_port: int | None = None
    if len(protocol_ports) == 1:
        protocol_port = next(iter(protocol_ports))
    elif len(protocol_ports) > 1:
        result.warnings.append("dubbo_protocol_port_ambiguous")
    if len(protocol_names) > 1:
        result.warnings.append("dubbo_protocol_name_ambiguous")
    triple_contracts, triple_warnings = _triple_contracts(
        proto_paths,
        root=root,
    )
    result.warnings.extend(triple_warnings)

    # 提供方注解必须在接口索引建好之后解析，否则跨文件 implements 可能找不到。
    for rel, text in sources:
        _scan_provider_annotations(
            rel=rel,
            text=text,
            interfaces=interfaces,
            classes=classes,
            class_index=class_index,
            providers=providers,
            protocol_port=protocol_port,
            protocols=protocol_names,
            triple_contracts=triple_contracts,
            result=result,
        )
    for item in xml_services:
        iface = interfaces.get(item["service"])
        triple_service = (
            _matches_triple_contract(item["service"], triple_contracts)
            if protocol_names == {"tri"}
            else None
        )
        if iface is None and not triple_service:
            result.unsupported.append(
                {
                    "file": item["file"],
                    "line": item["line"],
                    "reason": "dubbo_interface_in_jar",
                    "kind": "provider",
                    "detail": item["service"],
                }
            )
            continue
        if triple_service:
            records = _triple_provider_records(
                service=triple_service,
                contract=triple_contracts[triple_service],
                impl=item["ref"],
                group=_clean_group_version(item["group"]),
                version=_clean_group_version(item["version"]),
                direct_url=None,
                protocol_port=protocol_port,
                file=item["file"],
                line=item["line"],
            )
        else:
            assert iface is not None
            records = _provider_records(
                service=item["service"],
                impl=item["ref"],
                iface=iface,
                class_index=class_index,
                classes=classes,
                group=_clean_group_version(item["group"]),
                version=_clean_group_version(item["version"]),
                direct_url=None,
                protocol_port=protocol_port,
                file=item["file"],
                line=item["line"],
            )
        for record in records:
            _add_provider(providers, record, result)

    if protocol_names == {"tri"}:
        for record in providers.values():
            record.setdefault("protocol", "tri")
            record.setdefault("serialization", "hessian2")
            record.setdefault("compatibility", "native")

    result.providers = sorted(providers.values(), key=lambda item: item["endpoint"])
    result.checked = bool(sources or xml_services)

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
            group = _clean_group_version(group)
            version = _clean_group_version(version)
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
                parameter_types = candidates[0].params
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
