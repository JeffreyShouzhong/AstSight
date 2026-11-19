"""Java 类型契约索引：A 的源码优先，其次 A 依赖的 jar。

下游 B 的契约通常不在 A 的仓库里，但 A 调用 B 时一定得声明“响应解析成什么
类型”。这个类型就是 A 对 B 的期望，足以支撑 A 的测试。本模块负责把这个
类型解析成字段清单：

- 源码：扫描 `*.java` 的 class/record/interface/enum 声明与实例字段；
- jar：用 `javaclass` 解析依赖 jar 里的 class 文件（含泛型 Signature 属性）；
- 另外提供 Feign 接口扫描，供下游调用点识别后续使用。

解析顺序是源码优先、jar 兜底；找不到就返回 None，不猜字段。
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .javaclass import ClassFileError, ClassInfo, descriptor_to_type, parse_class
from .javadeps import discover_jars

_MAX_SOURCE_FILES = 20_000
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_CONTRACT_FIELDS = 256
# 父类字段合并的最大深度；Java 单继承链通常很浅，超过即视为异常并告警。
_MAX_INHERIT_DEPTH = 6
_SKIP_DIRS = {"target", "build", "out", ".git", "node_modules", "generated"}
_TEST_HINTS = ("src/test", "src\\test")

_CONTAINERS = {
    "List": "list",
    "ArrayList": "list",
    "LinkedList": "list",
    "Set": "list",
    "HashSet": "list",
    "LinkedHashSet": "list",
    "Collection": "list",
    "Iterable": "list",
    "Flux": "list",
    "Page": "page",
    "Slice": "page",
}
_WRAPPERS = {"Optional", "ResponseEntity", "Mono", "Callable", "CompletableFuture"}
# 没有业务字段但可静态判定的 JDK 响应类型；记为 scalar 契约，不当作解析失败。
_JDK_SCALARS = {
    "String": "java.lang.String",
    "Object": "java.lang.Object",
    "Integer": "java.lang.Integer",
    "Long": "java.lang.Long",
    "Boolean": "java.lang.Boolean",
    "Double": "java.lang.Double",
    "Float": "java.lang.Float",
    "Byte": "java.lang.Byte",
    "Short": "java.lang.Short",
    "Character": "java.lang.Character",
    "BigDecimal": "java.math.BigDecimal",
    "BigInteger": "java.math.BigInteger",
    "Void": "java.lang.Void",
}
# JDK 根本类没有业务字段，遇到即停止向上合并。
_JDK_BASES = {"java.lang.Object", "java.lang.Enum", "java.lang.Record"}


@dataclass(frozen=True)
class TypeField:
    name: str
    type: str
    annotations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "annotations": list(self.annotations),
        }


@dataclass
class TypeContract:
    name: str
    fqn: str
    kind: str  # source / jar / scalar
    origin: str
    fields: list[TypeField] = field(default_factory=list)
    container: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fqn": self.fqn,
            "kind": self.kind,
            "origin": self.origin,
            "container": self.container,
            "fields": [item.as_dict() for item in self.fields],
        }


@dataclass(frozen=True)
class FeignMethod:
    name: str
    http_method: str
    path: str
    response_type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "http_method": self.http_method,
            "path": self.path,
            "response_type": self.response_type,
        }


@dataclass
class FeignInterface:
    name: str
    fqn: str
    origin: str
    service: str = ""
    base_path: str = ""
    url: str = ""
    methods: list[FeignMethod] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fqn": self.fqn,
            "origin": self.origin,
            "service": self.service,
            "base_path": self.base_path,
            "url": self.url,
            "methods": [item.as_dict() for item in self.methods],
        }


@dataclass(frozen=True)
class _Decl:
    simple: str
    fqn: str
    path: Path
    kind: str
    start: int


@dataclass(frozen=True)
class _FileMeta:
    package: str
    imports: dict[str, str]
    wildcard: tuple[str, ...]


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth = max(0, depth - 1)
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    tail = text[start:]
    if tail.strip():
        parts.append(tail)
    return parts


def normalize_type_expr(expr: str) -> str:
    """把调用点里的类型表达式收敛成类型文本。"""
    text = str(expr or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    if text.endswith(".class"):
        text = text[: -len(".class")]
    match = re.match(
        r"new(?:[\w.]*)ParameterizedTypeReference<(.+)>\(\)(?:\{\})?$", text
    )
    if match:
        text = match.group(1)
    return text


def split_type(type_text: str) -> tuple[str, str | None, str]:
    """返回 (base, container, item_type)。数组按列表处理。"""
    text = normalize_type_expr(type_text)
    if not text:
        return "", None, ""
    if text.endswith("[]"):
        base = text[:-2]
        dims = 1
        while base.endswith("[]"):
            base = base[:-2]
            dims += 1
        return "List", "list", base + "[]" * (dims - 1)
    match = re.match(r"^([\w.$]+)<(.+)>$", text)
    if not match:
        return text, None, ""
    base = match.group(1)
    args = _split_top_level(match.group(2))
    simple = base.rsplit(".", 1)[-1]
    if simple in _WRAPPERS and args:
        return split_type(args[0])
    if simple in _CONTAINERS and args:
        item = args[0]
        inner_base, inner_container, _ = split_type(item)
        if inner_container is None:
            return base, _CONTAINERS[simple], item
        return base, _CONTAINERS[simple], inner_base
    return base, None, ""


class ContractIndex:
    """项目级契约索引；源码优先，jar 兜底。"""

    def __init__(
        self,
        project_root: str | Path,
        *,
        max_source_files: int = _MAX_SOURCE_FILES,
        max_jars: int = 200,
        m2_root: str | Path | None = None,
        gradle_root: str | Path | None = None,
    ):
        self.root = Path(project_root).resolve()
        self.max_source_files = max(1, int(max_source_files))
        self.max_jars = max(0, int(max_jars))
        self.m2_root = Path(m2_root) if m2_root else None
        self.gradle_root = Path(gradle_root) if gradle_root else None
        self.warnings: list[str] = []
        self._decls: dict[str, list[_Decl]] | None = None
        self._meta: dict[Path, _FileMeta] = {}
        self._texts: dict[Path, str] = {}
        self._cache: dict[str, TypeContract | None] = {}
        self._jar_entries: dict[str, tuple[Path, str]] | None = None
        self._jar_classes: dict[str, ClassInfo | None] = {}
        self._jar_indexed = False
        self._inherit_warned: set[str] = set()

    # ── 索引 ──────────────────────────────────────────────

    def _source_files(self) -> Iterable[Path]:
        count = 0
        for path in self.root.rglob("*.java"):
            rel = path.as_posix()
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if any(hint in rel for hint in _TEST_HINTS):
                continue
            yield path
            count += 1
            if count >= self.max_source_files:
                self.warnings.append(f"source_scan_truncated:{self.max_source_files}")
                return

    def _build_source_index(self) -> None:
        if self._decls is not None:
            return
        self._decls = {}
        type_re = re.compile(
            r"\b(class|interface|record|enum)\s+([A-Za-z_$][\w$]*)"
        )
        for path in self._source_files():
            text = self._read_source(path)
            if not text:
                continue
            package = _package_of(text)
            for match in type_re.finditer(text):
                simple = match.group(2)
                fqn = f"{package}.{simple}" if package else simple
                self._decls.setdefault(simple, []).append(
                    _Decl(
                        simple=simple,
                        fqn=fqn,
                        path=path,
                        kind=match.group(1),
                        start=match.start(),
                    )
                )

    def _read_source(self, path: Path) -> str:
        cached = self._texts.get(path)
        if cached is not None:
            return cached
        try:
            if path.stat().st_size > _MAX_SOURCE_BYTES:
                self.warnings.append(f"source_too_large:{path.name}")
                self._texts[path] = ""
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        self._texts[path] = text
        return text

    def _file_meta(self, path: Path) -> _FileMeta:
        cached = self._meta.get(path)
        if cached is not None:
            return cached
        text = self._read_source(path)
        imports: dict[str, str] = {}
        wildcard: list[str] = []
        for match in re.finditer(
            r"(?m)^\s*import\s+(?:static\s+)?([\w.]+)(\.\*)?\s*;", text
        ):
            name = match.group(1)
            if match.group(2):
                wildcard.append(name)
            else:
                imports[name.rsplit(".", 1)[-1]] = name
        meta = _FileMeta(
            package=_package_of(text),
            imports=imports,
            wildcard=tuple(wildcard),
        )
        self._meta[path] = meta
        return meta

    def _jar_index(self) -> dict[str, tuple[Path, str]]:
        if self._jar_entries is not None:
            return self._jar_entries
        entries: dict[str, tuple[Path, str]] = {}
        if self.max_jars <= 0:
            self._jar_entries = entries
            return entries
        try:
            jars = discover_jars(
                self.root,
                max_jars=self.max_jars,
                m2_root=self.m2_root,
                gradle_root=self.gradle_root,
            )
        except Exception as exc:
            self.warnings.append(f"jar_discovery_error:{type(exc).__name__}")
            jars = []
        for jar in jars:
            try:
                with zipfile.ZipFile(jar) as archive:
                    for name in archive.namelist():
                        if not name.endswith(".class"):
                            continue
                        if name.startswith("META-INF/") or "/META-INF/" in name:
                            continue
                        if name.endswith("module-info.class"):
                            continue
                        fqn = name[:-6].replace("/", ".")
                        entries.setdefault(fqn, (jar, name))
            except (OSError, zipfile.BadZipFile):
                self.warnings.append(f"jar_unreadable:{jar.name}")
        self._jar_entries = entries
        return entries

    # ── 解析 ──────────────────────────────────────────────

    def resolve(
        self, type_expr: str, *, context_file: str | Path | None = None
    ) -> TypeContract | None:
        base, container, item = split_type(type_expr)
        if not base:
            return None
        target = base
        if item and container in ("list", "page"):
            target = item if not item.endswith("[]") else item[:-2]
        simple = target.rsplit(".", 1)[-1]
        scalar_fqn = _JDK_SCALARS.get(simple)
        if scalar_fqn is not None:
            # JDK 标量没有业务字段，但契约是“已解析”的；Mock 侧按类型给默认值。
            return TypeContract(
                name=simple,
                fqn=scalar_fqn,
                kind="scalar",
                origin="jdk",
                fields=[],
                container=container,
            )
        fqn = self._resolve_fqn(target, context_file)
        if fqn is None:
            return None
        key = f"{fqn}|{container or ''}"
        if key in self._cache:
            contract = self._cache[key]
            if contract is not None:
                return TypeContract(
                    name=contract.name,
                    fqn=contract.fqn,
                    kind=contract.kind,
                    origin=contract.origin,
                    fields=list(contract.fields),
                    container=container,
                )
            return None
        contract = self._contract_for(fqn)
        self._cache[key] = contract
        if contract is None:
            return None
        return TypeContract(
            name=contract.name,
            fqn=contract.fqn,
            kind=contract.kind,
            origin=contract.origin,
            fields=list(contract.fields),
            container=container,
        )

    def resolve_many(
        self, exprs: Iterable[str], *, context_file: str | Path | None = None
    ) -> list[TypeContract]:
        result: list[TypeContract] = []
        seen: set[str] = set()
        for expr in exprs:
            contract = self.resolve(expr, context_file=context_file)
            if contract is None:
                continue
            key = f"{contract.fqn}|{contract.container or ''}"
            if key in seen:
                continue
            seen.add(key)
            result.append(contract)
        return result

    def _resolve_fqn(self, target: str, context_file: str | Path | None) -> str | None:
        if not target:
            return None
        if "." in target and self._looks_qualified(target):
            return target
        simple = target.rsplit(".", 1)[-1]
        if context_file is not None:
            path = Path(context_file)
            if not path.is_absolute():
                path = self.root / path
            if path.is_file():
                meta = self._file_meta(path)
                imported = meta.imports.get(simple)
                if imported:
                    return imported
                same_pkg = f"{meta.package}.{simple}" if meta.package else simple
                if self._has_decl(simple, same_pkg):
                    return same_pkg
                for namespace in meta.wildcard:
                    candidate = f"{namespace}.{simple}"
                    if self._has_decl(simple, candidate):
                        return candidate
                if "." in target:
                    outer = target.split(".", 1)[0]
                    imported_outer = meta.imports.get(outer)
                    if imported_outer:
                        return f"{imported_outer}.{target.split('.', 1)[1]}"
        self._build_source_index()
        decls = (self._decls or {}).get(simple, [])
        if len(decls) == 1:
            return decls[0].fqn
        if decls:
            # 同名类：优先非 test、路径最短的那个，其余视为歧义。
            decls = sorted(decls, key=lambda item: (len(item.path.parts), str(item.path)))
            return decls[0].fqn
        entries = self._jar_index()
        if target in entries:
            return target
        matches = [name for name in entries if name.rsplit(".", 1)[-1] == simple]
        if len(matches) == 1:
            return matches[0]
        return None

    def _looks_qualified(self, target: str) -> bool:
        head = target.split(".", 1)[0]
        return bool(head) and head[:1].islower()

    def _has_decl(self, simple: str, fqn: str) -> bool:
        self._build_source_index()
        return any(item.fqn == fqn for item in (self._decls or {}).get(simple, []))

    def _contract_for(
        self,
        fqn: str,
        *,
        _seen: frozenset[str] = frozenset(),
        _depth: int = 0,
    ) -> TypeContract | None:
        """解析类型契约，并递归合并父类字段（子类同名字段优先）。"""
        if fqn in _seen:
            # 继承环在 Java 编译期不可能出现；这里只做防御，不继续下钻。
            return None
        self._build_source_index()
        simple = fqn.rsplit(".", 1)[-1]
        seen = _seen | {fqn}
        for decl in (self._decls or {}).get(simple, []):
            if decl.fqn != fqn:
                continue
            fields = self._with_parent_fields(
                self._fields_from_source(decl),
                self._source_parent_fqn(decl),
                seen,
                _depth,
            )
            return TypeContract(
                name=simple,
                fqn=fqn,
                kind="source",
                origin=self._rel(decl.path),
                fields=fields,
            )
        class_info = self._class_from_jar(fqn)
        if class_info is None:
            return None
        fields = [
            TypeField(name=item.name, type=item.type_name())
            for item in class_info.fields
            if not item.is_static and not item.is_enum_constant
        ][:_MAX_CONTRACT_FIELDS]
        fields = self._with_parent_fields(
            fields, class_info.super_name, seen, _depth
        )
        jar, _entry = self._jar_index()[fqn]
        return TypeContract(
            name=class_info.simple_name,
            fqn=fqn,
            kind="jar",
            origin=str(jar),
            fields=fields,
        )

    def _with_parent_fields(
        self,
        fields: list[TypeField],
        parent_fqn: str | None,
        seen: frozenset[str],
        depth: int,
    ) -> list[TypeField]:
        if not parent_fqn or parent_fqn in _JDK_BASES:
            return fields
        if depth >= _MAX_INHERIT_DEPTH:
            if parent_fqn not in self._inherit_warned:
                self._inherit_warned.add(parent_fqn)
                self.warnings.append(f"inherit_depth_exceeded:{parent_fqn}")
            return fields
        parent = self._contract_for(parent_fqn, _seen=seen, _depth=depth + 1)
        if parent is None or not parent.fields:
            return fields
        names = {item.name for item in fields}
        merged = list(fields)
        for item in parent.fields:
            if item.name in names:
                # 子类同名字段（shadow）覆盖父类，保留子类声明。
                continue
            names.add(item.name)
            merged.append(item)
            if len(merged) >= _MAX_CONTRACT_FIELDS:
                break
        return merged

    def _source_parent_fqn(self, decl: _Decl) -> str | None:
        if decl.kind != "class":
            return None
        raw = self._read_source(decl.path)
        if not raw:
            return None
        header = _strip_comments(raw[decl.start :])
        brace = header.find("{")
        if brace >= 0:
            header = header[:brace]
        # 跳过类型参数声明，避免把 <T extends Base> 的边界当成父类。
        match = re.search(
            r"\bclass\s+[\w$]+\s*(?:<[^{]*?>)?\s*extends\s+([\w$.]+)",
            header,
            re.S,
        )
        if not match:
            return None
        return self._resolve_fqn(match.group(1), decl.path)

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _fields_from_source(self, decl: _Decl) -> list[TypeField]:
        raw = self._read_source(decl.path)
        if not raw:
            return []
        text = _strip_comments(raw[decl.start :])
        if decl.kind == "record":
            return _record_fields(text[:400])
        if decl.kind == "enum":
            return []
        brace = text.find("{")
        if brace < 0:
            return []
        body = _class_body(text, brace)
        if body is None:
            return []
        return _class_fields(body)

    def _class_from_jar(self, fqn: str) -> ClassInfo | None:
        if fqn in self._jar_classes:
            return self._jar_classes[fqn]
        entry = self._jar_index().get(fqn)
        if entry is None:
            self._jar_classes[fqn] = None
            return None
        jar, name = entry
        info: ClassInfo | None
        try:
            with zipfile.ZipFile(jar) as archive:
                data = archive.read(name)
            info = parse_class(data)
        except (OSError, KeyError, zipfile.BadZipFile, ClassFileError):
            info = None
        self._jar_classes[fqn] = info
        return info


def _package_of(text: str) -> str:
    match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
    return match.group(1) if match else ""


def _class_body(text: str, brace: int) -> str | None:
    depth = 0
    index = brace
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1 : index]
        index += 1
    return None


_FIELD_RE = re.compile(
    r"^\s*(?P<mods>(?:(?:public|protected|private|static|final|transient|volatile)\s+)*)"
    r"(?P<type>[\w$.<>\[\], ?]+?)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?:=[^;]*)?;$",
    re.S,
)


def _top_level_segments(body: str) -> list[str]:
    segments: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(body):
        char = body[index]
        if char == '"':
            index += 1
            while index < len(body):
                if body[index] == "\\":
                    index += 2
                    continue
                if body[index] == '"':
                    break
                index += 1
        elif char == "'":
            index += 1
            while index < len(body) and body[index] != "'":
                index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                segments.append(body[start : index + 1])
                start = index + 1
        elif char == ";" and depth == 0:
            segments.append(body[start : index + 1])
            start = index + 1
        index += 1
    tail = body[start:]
    if tail.strip():
        segments.append(tail)
    return segments


def _class_fields(body: str) -> list[TypeField]:
    fields: list[TypeField] = []
    for segment in _top_level_segments(body):
        annotations = tuple(
            re.findall(
                r"@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?",
                segment,
            )
        )
        cleaned = re.sub(
            r"@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?",
            " ",
            segment,
        ).strip()
        if "(" in cleaned:
            continue
        match = _FIELD_RE.match(cleaned)
        if not match:
            continue
        if "static" in match.group("mods"):
            continue
        type_text = match.group("type").strip().lstrip("@").strip()
        if not type_text or type_text in {"return", "new"}:
            continue
        fields.append(
            TypeField(
                name=match.group("name"),
                type=type_text,
                annotations=annotations,
            )
        )
        if len(fields) >= _MAX_CONTRACT_FIELDS:
            break
    return fields


def _record_fields(head: str) -> list[TypeField]:
    match = re.search(r"record\s+\w+\s*\((.*?)\)\s*(?:implements|\{|;)", head, re.S)
    if not match:
        return []
    fields: list[TypeField] = []
    for item in _split_top_level(match.group(1)):
        annotations = tuple(
            re.findall(r"@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?", item)
        )
        cleaned = re.sub(
            r"@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?",
            " ",
            item,
        ).strip()
        if not cleaned:
            continue
        parts = cleaned.rsplit(None, 1)
        if len(parts) != 2:
            continue
        fields.append(
            TypeField(
                name=parts[1].strip(),
                type=parts[0].strip(),
                annotations=annotations,
            )
        )
        if len(fields) >= _MAX_CONTRACT_FIELDS:
            break
    return fields


# ───────────────────────── Feign 接口 ─────────────────────────

_FEIGN_CLIENT = re.compile(r"@FeignClient\s*\(([^)]*)\)", re.S)
_MAPPING_ANY = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b")
# 读 jar 前按类名收窄：Feign 接口通常以这些后缀命名。
_FEIGN_CLASS_HINT = re.compile(r"(client|api|feign|remote|sdk)$", re.I)
_MAX_FEIGN_CLASS_READS = 4_000


def _named_annotation_value(text: str, name: str) -> str:
    """只读显式 `name = "..."`，不做位置参数回退。"""
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]", text
    )
    return match.group(1) if match else ""


def _annotation_value(text: str, *names: str) -> str:
    for name in names:
        match = re.search(
            rf"\b{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]", text
        )
        if match:
            return match.group(1)
    quoted = re.search(r"['\"]([^'\"]+)['\"]", text)
    return quoted.group(1) if quoted else ""


def _split_params(text: str) -> list[str]:
    result: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            result.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        result.append("".join(current).strip())
    return [item for item in result if item]


def _method_return_type(head: str) -> str:
    """从“返回类型 + 方法名”里取返回类型，不带参数列表。"""
    cleaned = re.sub(
        r"\b(?:public|protected|private|static|final|abstract|default)\b", " ", head
    )
    cleaned = re.sub(r"@[\w.]+(?:\([^)]*\))?", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    match = re.match(r"(.+?)\s+[A-Za-z_$][\w$]*(?:\s*\(|$)", cleaned)
    return match.group(1).strip() if match else ""


def _match_paren(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _feign_methods(body: str) -> list[FeignMethod]:
    """按括号配对读接口方法，避免参数里的注解括号打断解析。"""
    methods: list[FeignMethod] = []
    for match in _MAPPING_ANY.finditer(body):
        cursor = match.end()
        probe = cursor
        while probe < len(body) and body[probe].isspace():
            probe += 1
        annotation_args = ""
        if probe < len(body) and body[probe] == "(":
            close = _match_paren(body, probe)
            if close < 0:
                continue
            annotation_args = body[probe + 1 : close]
            cursor = close + 1
        paren = body.find("(", cursor)
        if paren < 0:
            continue
        end = _match_paren(body, paren)
        if end < 0:
            continue
        head = body[cursor:paren].strip()
        name_match = re.search(r"([A-Za-z_$][\w$]*)\s*$", head)
        if not name_match:
            continue
        verb = match.group(1).upper()
        if verb == "REQUEST":
            # 兼容 `method = RequestMethod.GET` 与静态导入后的 `method = GET`。
            verb_match = re.search(
                r"method\s*=\s*\{?\s*(?:RequestMethod\.)?([A-Z]+)\b",
                annotation_args,
            )
            verb = verb_match.group(1).upper() if verb_match else ""
        methods.append(
            FeignMethod(
                name=name_match.group(1),
                http_method=verb,
                path=_annotation_value(annotation_args, "value", "path"),
                response_type=_method_return_type(head),
            )
        )
    return methods


def scan_feign_interfaces(
    project_root: str | Path,
    *,
    index: ContractIndex | None = None,
    include_jars: bool = True,
) -> list[FeignInterface]:
    """从源码与依赖 jar 收集 Feign 接口。只做只读解析，不执行。"""
    root = Path(project_root).resolve()
    owner = index or ContractIndex(root)
    found: dict[str, FeignInterface] = {}
    for path in owner._source_files():
        text = owner._read_source(path)
        if "@FeignClient" not in text:
            continue
        for item in _feign_from_source(text, owner._rel(path)):
            found.setdefault(item.fqn, item)
    if include_jars:
        try:
            jars = discover_jars(
                owner.root,
                max_jars=owner.max_jars,
                m2_root=owner.m2_root,
                gradle_root=owner.gradle_root,
            )
        except Exception as exc:
            owner.warnings.append(f"jar_discovery_error:{type(exc).__name__}")
            jars = []
        reads = 0
        truncated = False
        for jar in jars:
            if reads >= _MAX_FEIGN_CLASS_READS:
                truncated = True
                break
            try:
                with zipfile.ZipFile(jar) as archive:
                    for entry in archive.namelist():
                        if not entry.endswith(".class") or "$" in entry:
                            continue
                        fqn = entry[:-6].replace("/", ".")
                        if fqn in found:
                            continue
                        if not _FEIGN_CLASS_HINT.search(fqn.rsplit(".", 1)[-1]):
                            continue
                        if reads >= _MAX_FEIGN_CLASS_READS:
                            truncated = True
                            break
                        reads += 1
                        try:
                            data = archive.read(entry)
                        except (OSError, KeyError):
                            continue
                        if b"FeignClient" not in data:
                            continue
                        try:
                            info = parse_class(data)
                        except ClassFileError:
                            continue
                        if "FeignClient" not in info.annotations:
                            continue
                        found[fqn] = _feign_from_class(info, str(jar))
            except (OSError, zipfile.BadZipFile):
                owner.warnings.append(f"jar_unreadable:{jar.name}")
                continue
            if truncated:
                break
        if truncated:
            owner.warnings.append(f"feign_scan_truncated:{_MAX_FEIGN_CLASS_READS}")
    return sorted(found.values(), key=lambda item: item.fqn)


def _feign_from_source(text: str, origin: str) -> list[FeignInterface]:
    result: list[FeignInterface] = []
    for match in _FEIGN_CLIENT.finditer(text):
        attrs = match.group(1)
        service = _annotation_value(attrs, "name", "value", "serviceId")
        tail = text[match.end() : match.end() + 400]
        interface_match = re.search(r"interface\s+([A-Za-z_$][\w$]*)", tail)
        if not interface_match:
            continue
        name = interface_match.group(1)
        package = _package_of(text)
        fqn = f"{package}.{name}" if package else name
        body_start = text.find("{", match.end() + interface_match.end())
        body = _class_body(text, body_start) if body_start >= 0 else ""
        methods = _feign_methods(body or "")
        result.append(
            FeignInterface(
                name=name,
                fqn=fqn,
                origin=origin,
                service=service,
                base_path=_named_annotation_value(attrs, "path"),
                url=_named_annotation_value(attrs, "url"),
                methods=methods,
            )
        )
    return result


def _scalar_annotation(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _scalar_annotation(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return ""


def _feign_from_class(info: ClassInfo, origin: str) -> FeignInterface:
    attrs = info.annotations.get("FeignClient", {})
    service = _scalar_annotation(
        attrs.get("name") or attrs.get("value") or attrs.get("serviceId") or ""
    )
    methods: list[FeignMethod] = []
    for method in info.methods:
        mapping = None
        verb = ""
        path = ""
        for key in ("GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping", "RequestMapping"):
            if key in method.annotations:
                mapping = method.annotations[key]
                if key != "RequestMapping":
                    verb = key[: -len("Mapping")].upper()
                break
        if mapping is None:
            continue
        if not verb:
            raw = mapping.get("method")
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if isinstance(raw, dict):
                verb = str(raw.get("value") or "")
            verb = verb.replace("RequestMethod.", "").upper()
        path = _scalar_annotation(mapping.get("value") or mapping.get("path") or "")
        methods.append(
            FeignMethod(
                name=method.name,
                http_method=verb,
                path=path,
                response_type=_descriptor_return_type(method.descriptor),
            )
        )
    return FeignInterface(
        name=info.simple_name,
        fqn=info.name,
        origin=origin,
        service=service,
        base_path=_scalar_annotation(attrs.get("path") or ""),
        url=_scalar_annotation(attrs.get("url") or ""),
        methods=methods,
    )


def _descriptor_return_type(descriptor: str) -> str:
    match = re.match(r"^\([^)]*\)(.+)$", descriptor or "")
    return descriptor_to_type(match.group(1)) if match else ""


__all__ = [
    "ContractIndex",
    "FeignInterface",
    "FeignMethod",
    "TypeContract",
    "TypeField",
    "normalize_type_expr",
    "scan_feign_interfaces",
    "split_type",
]
