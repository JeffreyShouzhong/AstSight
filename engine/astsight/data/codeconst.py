"""C5a 取值语义的代码常量来源：Java / Python / TypeScript 枚举。

只做静态读取，不执行代码、不调用模型。枚举到列的映射必须可解释：

- 数据库枚举值全部能在代码枚举里找到（强匹配）；
- 列注释里明确写了枚举类型名；
- 列名与枚举名按「表名 + 状态/类型」命名约定唯一对应。

仍然只生成 `inferred` 候选；对不上、多处匹配或与数据库枚举冲突时告警并放弃。
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from contracts import ColumnCatalog, TableCatalog

from .annotations import merge_value

MAX_FILES = 5_000
MAX_FILE_BYTES = 1_000_000
MAX_ENUMS = 2_000
MAX_CONSTANTS_PER_ENUM = 500

_SCAN_EXTENSIONS = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
}
_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".gradle",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "target",
    "build",
    "dist",
    "out",
    "bin",
    "obj",
    "coverage",
}
_JAVA_ENUM_RE = re.compile(r"\benum\s+([A-Za-z_$][\w$]*)")
_TS_ENUM_RE = re.compile(
    r"(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)"
)
_JAVA_IDENT_RE = re.compile(r"^@?[A-Za-z_$][\w$]*")
_JAVA_ANNOTATION_RE = re.compile(r"^@[A-Za-z_$][\w$]*(?:\([^)]*\))?\s*")
_INT_RE = re.compile(r"^-?\d+$")
_DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")
_NORM_RE = re.compile(r"[^a-z0-9]+")
_ENUM_SUFFIXES = ("status", "state", "type", "kind", "code", "enum", "category", "level", "flag")
_TYPE_SUFFIXES = ("enum", "type", "code")
_UNKNOWN = object()


@dataclass(frozen=True)
class EnumConstant:
    name: str
    value: Any
    meaning: str = ""


@dataclass(frozen=True)
class CodeEnum:
    name: str
    language: str
    path: str
    constants: tuple[EnumConstant, ...]

    @property
    def source(self) -> str:
        return f"code:{self.path}"


@dataclass
class CodeScanResult:
    enums: list[CodeEnum] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)


@dataclass
class CodeFoldResult:
    warnings: list[str] = field(default_factory=list)
    applied: int = 0
    used: list[str] = field(default_factory=list)


class CodeScanError(ValueError):
    """扫描路径或规模不合法。"""


def scan_code_enums(
    project: str | Path,
    roots: Iterable[str | Path],
) -> CodeScanResult:
    project_path = Path(project).resolve()
    result = CodeScanResult()
    seen_files: set[Path] = set()
    for raw in roots:
        root = _resolve_root(project_path, raw)
        if root is None:
            result.warnings.append(f"code_path_missing:{raw}")
            continue
        files = [root] if root.is_file() else list(_iter_source_files(root))
        for path in files:
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            if len(seen_files) > MAX_FILES:
                raise CodeScanError(f"扫描文件超过上限 {MAX_FILES}")
            if resolved.suffix.lower() not in _SCAN_EXTENSIONS:
                continue
            _scan_file(project_path, resolved, result)
            if len(result.enums) > MAX_ENUMS:
                raise CodeScanError(f"枚举数量超过上限 {MAX_ENUMS}")
    result.enums.sort(key=lambda item: (item.path, item.name))
    result.files.sort()
    return result


def _resolve_root(project: Path, raw: str | Path) -> Path | None:
    path = Path(raw)
    if not path.is_absolute():
        candidate = (Path.cwd() / path).resolve()
        path = candidate if candidate.exists() else (project / path).resolve()
    path = path.resolve()
    if not path.exists():
        return None
    return path


def _iter_source_files(root: Path) -> Iterator[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            name for name in dirs if name.lower() not in _EXCLUDED_DIRS
        )
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.lower() in _SCAN_EXTENSIONS:
                yield path


def _scan_file(project: Path, path: Path, result: CodeScanResult) -> None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            result.warnings.append(f"code_file_too_large:{_rel(project, path)}")
            return
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        result.warnings.append(f"code_file_unreadable:{_rel(project, path)}:{exc}")
        return
    language = _SCAN_EXTENSIONS[path.suffix.lower()]
    relative = _rel(project, path)
    if language == "python":
        enums, warnings = _scan_python(text, relative)
    elif language == "java":
        enums, warnings = _scan_java(text, relative)
    else:
        enums, warnings = _scan_typescript(text, relative)
    result.enums.extend(enums)
    result.warnings.extend(f"{relative}:{item}" for item in warnings)
    result.files.append(relative)
    if enums:
        result.languages[language] = result.languages.get(language, 0) + 1


def _rel(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return path.name


# ---------------------------------------------------------------------------
# 词法辅助：所有解析器共用，忽略字符串与注释里的括号/逗号。
# ---------------------------------------------------------------------------


def _is_code_position(text: str, index: int) -> bool:
    """判断 ``index`` 是否在字符串或注释之外，避免把注释里的 enum 当成声明。"""
    quote: str | None = None
    position = 0
    while position < index:
        char = text[position]
        if quote:
            if char == "\\":
                position += 2
                continue
            if char == quote:
                quote = None
            position += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            position += 1
            continue
        if text.startswith("//", position):
            end = text.find("\n", position, index)
            if end < 0:
                return False
            position = end + 1
            continue
        if text.startswith("/*", position):
            end = text.find("*/", position + 2, index)
            if end < 0:
                return False
            position = end + 2
            continue
        position += 1
    return quote is None


def _split_line_comment(text: str, marker: str = "//") -> tuple[str, str]:
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if text.startswith(marker, index):
            return text[:index], text[index + len(marker) :].strip()
        index += 1
    return text, ""


def _find_matching(text: str, start: int) -> int:
    if start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            if end < 0:
                return -1
            index = end + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return -1
            index = end + 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _split_top_level(text: str, separator: str = ",", stop: str = "") -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == "\\":
                if index + 1 < len(text):
                    current.append(text[index + 1])
                    index += 2
                    continue
                index += 1
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            current.append("\n")
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if depth == 0 and stop and char == stop:
            break
        if depth == 0 and char == separator:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _literal(raw: str) -> Any:
    text = (raw or "").strip().rstrip(";").strip()
    if not text:
        return _UNKNOWN
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"', "`"}:
        return text[1:-1]
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in {"null", "none", "undefined"}:
        return None
    if _INT_RE.match(text):
        if len(text.lstrip("-")) > 1 and text.lstrip("-").startswith("0"):
            return text
        try:
            return int(text)
        except ValueError:
            return text
    if _DECIMAL_RE.match(text):
        try:
            return float(text)
        except ValueError:
            return text
    return _UNKNOWN


# ---------------------------------------------------------------------------
# Python：用 ast，不执行代码。
# ---------------------------------------------------------------------------


def _scan_python(text: str, path: str) -> tuple[list[CodeEnum], list[str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [], [f"python_parse_error:{exc.lineno}"]
    aliases = _python_enum_aliases(tree)
    lines = text.splitlines()
    out: list[CodeEnum] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _python_is_enum(node, aliases):
            continue
        constants: list[EnumConstant] = []
        for statement in node.body:
            name, value_node, lineno = _python_assignment(statement)
            if not name or value_node is None:
                continue
            value = _python_literal(value_node)
            if value is _UNKNOWN:
                continue
            meaning = _python_trailing_comment(lines, lineno - 1)
            constants.append(EnumConstant(name=name, value=value, meaning=meaning))
            if len(constants) >= MAX_CONSTANTS_PER_ENUM:
                break
        if constants:
            out.append(
                CodeEnum(
                    name=node.name,
                    language="python",
                    path=path,
                    constants=tuple(constants),
                )
            )
    return out, []


def _python_enum_aliases(tree: ast.AST) -> set[str]:
    names = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum"}
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "enum":
            for alias in node.names:
                if alias.name in names:
                    aliases.add(alias.asname or alias.name)
    return names | aliases


def _python_is_enum(node: ast.ClassDef, aliases: set[str]) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in aliases:
            return True
        if isinstance(base, ast.Attribute) and base.attr in aliases:
            return True
    return False


def _python_assignment(
    node: ast.stmt,
) -> tuple[str, ast.expr | None, int]:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id, node.value, node.lineno
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, node.value, node.lineno
    return "", None, 0


def _python_literal(node: ast.expr) -> Any:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, int, float, bool)
    ):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        return -node.operand.value
    return _UNKNOWN


def _python_trailing_comment(lines: list[str], index: int) -> str:
    if index < 0 or index >= len(lines):
        return ""
    _code, comment = _split_line_comment(lines[index], "#")
    return comment[:500]


# ---------------------------------------------------------------------------
# Java：枚举常量可带构造参数；无参数时用常量名作为取值。
# ---------------------------------------------------------------------------


def _scan_java(text: str, path: str) -> tuple[list[CodeEnum], list[str]]:
    out: list[CodeEnum] = []
    warnings: list[str] = []
    for match in _JAVA_ENUM_RE.finditer(text):
        if not _is_code_position(text, match.start()):
            continue
        name = match.group(1)
        brace = text.find("{", match.end())
        if brace < 0:
            warnings.append(f"java_enum_unclosed:{name}")
            continue
        end = _find_matching(text, brace)
        if end < 0:
            warnings.append(f"java_enum_unclosed:{name}")
            continue
        body = text[brace + 1 : end]
        constants = _java_constants(body)
        if constants:
            out.append(
                CodeEnum(
                    name=name,
                    language="java",
                    path=path,
                    constants=tuple(constants),
                )
            )
    return out, warnings


def _entries_with_comments(body: str, *, stop: str = ";") -> list[tuple[str, str]]:
    """按顶层逗号切分枚举项，并把 `, // 含义` 归到前一项。"""
    out: list[tuple[str, str]] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    pending = ""
    stops = {","} | ({stop} if stop else set())
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            current.append(char)
            if char == "\\" and index + 1 < len(body):
                current.append(body[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
            index += 1
            continue
        if body.startswith("//", index):
            end = body.find("\n", index)
            end = len(body) if end < 0 else end
            comment = body[index + 2 : end].strip()
            if not "".join(current).strip() and out:
                out[-1] = (out[-1][0], comment)
            elif comment:
                pending = pending or comment
            index = end
            continue
        if body.startswith("/*", index):
            end = body.find("*/", index + 2)
            index = len(body) if end < 0 else end + 2
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if depth == 0 and char in stops:
            text = "".join(current).strip()
            if text:
                out.append((text, pending))
            current = []
            pending = ""
            index += 1
            if char == stop:
                break
            continue
        current.append(char)
        index += 1
    text = "".join(current).strip()
    if text:
        out.append((text, pending))
    return out


def _java_constants(body: str) -> list[EnumConstant]:
    out: list[EnumConstant] = []
    for entry, comment in _entries_with_comments(body):
        entry = entry.strip()
        while entry.startswith("@"):
            entry = _JAVA_ANNOTATION_RE.sub("", entry, count=1).strip()
        match = _JAVA_IDENT_RE.match(entry)
        if match is None:
            continue
        name = match.group(0)
        rest = entry[match.end() :].strip()
        value: Any = name
        if rest.startswith("("):
            inner, _ = _paren_content(rest)
            args = _split_top_level(inner, separator=",") if inner else []
            if args:
                literal = _literal(args[0])
                if literal is not _UNKNOWN:
                    value = literal
        out.append(EnumConstant(name=name, value=value, meaning=comment[:500]))
        if len(out) >= MAX_CONSTANTS_PER_ENUM:
            break
    return out


def _paren_content(text: str) -> tuple[str, int]:
    if not text.startswith("("):
        return "", -1
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[1:index], index
    return text[1:], -1


# ---------------------------------------------------------------------------
# TypeScript：支持字符串/数字初始化；无初始化按 TS 规则顺延编号。
# ---------------------------------------------------------------------------


def _scan_typescript(text: str, path: str) -> tuple[list[CodeEnum], list[str]]:
    out: list[CodeEnum] = []
    warnings: list[str] = []
    for match in _TS_ENUM_RE.finditer(text):
        if not _is_code_position(text, match.start()):
            continue
        name = match.group(1)
        brace = text.find("{", match.end())
        if brace < 0:
            warnings.append(f"ts_enum_unclosed:{name}")
            continue
        end = _find_matching(text, brace)
        if end < 0:
            warnings.append(f"ts_enum_unclosed:{name}")
            continue
        constants = _ts_constants(text[brace + 1 : end])
        if constants:
            out.append(
                CodeEnum(
                    name=name,
                    language="typescript",
                    path=path,
                    constants=tuple(constants),
                )
            )
    return out, warnings


def _ts_constants(body: str) -> list[EnumConstant]:
    out: list[EnumConstant] = []
    next_number: int | None = 0
    for raw, comment in _entries_with_comments(body, stop=""):
        entry = raw.strip()
        if not entry:
            continue
        name, _, initializer = entry.partition("=")
        name = name.strip()
        if not re.match(r"^[A-Za-z_$][\w$]*$", name):
            continue
        value: Any = _UNKNOWN
        if initializer.strip():
            value = _literal(initializer)
            next_number = (
                int(value) + 1
                if isinstance(value, int) and not isinstance(value, bool)
                else None
            )
        elif next_number is not None:
            value = next_number
            next_number += 1
        if value is _UNKNOWN or value is None:
            continue
        out.append(EnumConstant(name=name, value=value, meaning=comment[:500]))
        if len(out) >= MAX_CONSTANTS_PER_ENUM:
            break
    return out


# ---------------------------------------------------------------------------
# 映射：代码枚举 → 目录列。
# ---------------------------------------------------------------------------


def fold_code_enums(
    tables: Iterable[TableCatalog],
    enums: Iterable[CodeEnum],
) -> CodeFoldResult:
    result = CodeFoldResult()
    enum_list = list(enums)
    if not enum_list:
        return result
    for table in tables:
        table_norm = _norm(table.name.split(".")[-1])
        for column in table.columns:
            chosen = _choose_enum(
                table.name, table_norm, column, enum_list, result.warnings
            )
            if chosen is None:
                continue
            applied = _apply_enum(table, column, chosen, result.warnings)
            if applied:
                result.applied += applied
                if chosen.source not in result.used:
                    result.used.append(chosen.source)
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


def _choose_enum(
    table_name: str,
    table_norm: str,
    column: ColumnCatalog,
    enums: list[CodeEnum],
    warnings: list[str],
) -> CodeEnum | None:
    scored: list[tuple[int, CodeEnum]] = []
    for enum in enums:
        rank = _match_rank(table_norm, column, enum)
        if rank:
            scored.append((rank, enum))
    if not scored:
        return None
    best = max(rank for rank, _enum in scored)
    top = [enum for rank, enum in scored if rank == best]
    if len(top) > 1:
        names = ",".join(sorted(enum.name for enum in top))
        warnings.append(
            f"code_enum_ambiguous:{table_name}.{column.name}:{names}"
        )
        return None
    return top[0]


def _match_rank(table_norm: str, column: ColumnCatalog, enum: CodeEnum) -> int:
    if _value_match(column, enum):
        base = 30
    elif _comment_match(column, enum):
        base = 20
    elif _name_match(table_norm, column.name, enum.name):
        base = 10
    else:
        return 0
    # 多个同名候选时，取值类型与列类型一致的枚举优先；只做加分，不单独构成匹配。
    return base + _type_affinity(column, enum)


def _type_affinity(column: ColumnCatalog, enum: CodeEnum) -> int:
    values = [item.value for item in enum.constants]
    if not values:
        return 0
    upper_type = (column.type or "").upper()
    if any(token in upper_type for token in ("CHAR", "TEXT", "CLOB", "ENUM", "UUID")):
        if all(isinstance(value, str) for value in values):
            return 3
        if all(_numeric(value) for value in values):
            return 1
        return 0
    if any(token in upper_type for token in ("INT", "SERIAL")):
        if all(_integer(value) for value in values):
            return 3
        if all(isinstance(value, str) and _INT_RE.match(value) for value in values):
            return 2
        return 0
    if any(
        token in upper_type
        for token in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")
    ):
        if all(_numeric(value) for value in values):
            return 3
        return 0
    if "BOOL" in upper_type:
        return 3 if all(isinstance(value, bool) for value in values) else 0
    return 0


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _value_match(column: ColumnCatalog, enum: CodeEnum) -> bool:
    if not column.enum_values:
        return False
    database = {str(value).lower() for value in column.enum_values}
    code = {str(item.value).lower() for item in enum.constants} | {
        item.name.lower() for item in enum.constants
    }
    return bool(database) and database <= code


def _comment_match(column: ColumnCatalog, enum: CodeEnum) -> bool:
    comment = _norm(column.comment)
    return bool(comment) and _norm(enum.name) in comment


def _name_match(table_norm: str, column_name: str, enum_name: str) -> bool:
    column_norm = _norm(column_name)
    enum_norm = _norm(enum_name)
    if not column_norm or not enum_norm:
        return False
    if column_norm == enum_norm:
        return True
    prefixes: list[str] = []
    if (
        enum_norm.endswith(column_norm)
        and len(enum_norm) > len(column_norm)
        and column_norm in _ENUM_SUFFIXES
    ):
        prefixes.append(enum_norm[: -len(column_norm)])
    for suffix in _TYPE_SUFFIXES:
        if not enum_norm.endswith(suffix):
            continue
        inner = enum_norm[: -len(suffix)]
        if inner.endswith(column_norm) and len(inner) > len(column_norm):
            prefixes.append(inner[: -len(column_norm)])
    return any(
        prefix
        and (
            prefix == table_norm
            or prefix.startswith(table_norm)
            or table_norm.startswith(prefix)
        )
        for prefix in prefixes
    )


def _apply_enum(
    table: TableCatalog,
    column: ColumnCatalog,
    enum: CodeEnum,
    warnings: list[str],
) -> int:
    applied = 0
    if column.enum_values:
        missing: list[str] = []
        for database_value in column.enum_values:
            constant = _match_constant(database_value, enum)
            if constant is None:
                missing.append(str(database_value))
                continue
            merge_value(
                table.name,
                column,
                database_value,
                constant.meaning or constant.name,
                "",
                warnings,
                source=enum.source,
            )
            applied += 1
        if missing:
            warnings.append(
                "code_enum_value_mismatch:"
                f"{table.name}.{column.name}:{','.join(missing)}"
            )
        return applied
    for constant in enum.constants:
        value = _coerce_code_value(constant.value, column)
        if value is None:
            warnings.append(
                "code_enum_value_unsupported:"
                f"{table.name}.{column.name}:{constant.name}"
            )
            continue
        merge_value(
            table.name,
            column,
            value,
            constant.meaning or constant.name,
            "",
            warnings,
            source=enum.source,
        )
        applied += 1
    return applied


def _match_constant(value: Any, enum: CodeEnum) -> EnumConstant | None:
    target = str(value)
    for constant in enum.constants:
        if str(constant.value) == target or constant.name == target:
            return constant
    lowered = target.lower()
    for constant in enum.constants:
        if constant.name.lower() == lowered:
            return constant
    return None


def _coerce_code_value(value: Any, column: ColumnCatalog) -> Any:
    if value is None:
        return None
    upper_type = (column.type or "").upper()
    if any(token in upper_type for token in ("INT", "SERIAL")):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and _INT_RE.match(value):
            return int(value)
        return None
    if any(
        token in upper_type
        for token in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")
    ):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and (
            _INT_RE.match(value) or _DECIMAL_RE.match(value)
        ):
            return float(value)
        return None
    if "BOOL" in upper_type:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        return None
    if any(
        token in upper_type
        for token in ("CHAR", "TEXT", "CLOB", "ENUM", "UUID")
    ):
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    return value


def _norm(value: str) -> str:
    return _NORM_RE.sub("", (value or "").lower())


__all__ = [
    "CodeEnum",
    "CodeFoldResult",
    "CodeScanError",
    "CodeScanResult",
    "EnumConstant",
    "fold_code_enums",
    "scan_code_enums",
]
