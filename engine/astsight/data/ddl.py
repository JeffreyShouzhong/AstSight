"""DDL 解析（C5a 的结构事实源）。

v1 覆盖常见 `CREATE TABLE` 与 `ALTER TABLE ... ADD CONSTRAINT/COLUMN`：
列、类型、NOT NULL、DEFAULT、PK、UNIQUE、REFERENCES、CHECK(IN)、注释。
无法识别的语句只进 warnings，不猜结构。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_DDL_BYTES = 5 * 1024 * 1024
MAX_TABLES = 500
MAX_COLUMNS_PER_TABLE = 500

_CONSTRAINT_KEYWORDS = {
    "CONSTRAINT",
    "PRIMARY",
    "NOT",
    "NULL",
    "DEFAULT",
    "UNIQUE",
    "REFERENCES",
    "CHECK",
    "COLLATE",
    "GENERATED",
    "AUTOINCREMENT",
    "AUTO_INCREMENT",
    "COMMENT",
    "ON",
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_ENUM_RE = re.compile(r"^ENUM\s*\((.*)\)$", re.IGNORECASE | re.DOTALL)
_CHECK_IN_RE = re.compile(
    r"CHECK\s*\(\s*[\w\"`.\[\]]+\s+IN\s*\((.*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


class DDLError(ValueError):
    """DDL 不可解析/超出资源上限。"""


@dataclass
class ParsedColumn:
    name: str
    type: str = ""
    nullable: bool = True
    default: Any = None
    primary_key: bool = False
    unique: bool = False
    comment: str = ""
    enum_values: list[Any] = field(default_factory=list)


@dataclass
class ParsedForeignKey:
    child_table: str
    child_columns: list[str]
    parent_table: str
    parent_columns: list[str]


@dataclass
class ParsedTable:
    name: str
    columns: list[ParsedColumn] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ParsedForeignKey] = field(default_factory=list)
    comment: str = ""

    def column_index(self) -> dict[str, ParsedColumn]:
        return {column.name: column for column in self.columns}


@dataclass
class ParsedDDL:
    tables: list[ParsedTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def table_index(self) -> dict[str, ParsedTable]:
        return {table.name: table for table in self.tables}

    @property
    def foreign_keys(self) -> list[ParsedForeignKey]:
        return [fk for table in self.tables for fk in table.foreign_keys]


def _strip_comments(text: str) -> str:
    out: list[str] = []
    index = 0
    length = len(text)
    quote: str | None = None
    while index < length:
        char = text[index]
        if quote:
            out.append(char)
            if char == quote:
                if index + 1 < length and text[index + 1] == quote:
                    out.append(text[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "-" and text.startswith("--", index):
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def split_statements(text: str) -> list[str]:
    cleaned = _strip_comments(text)
    statements: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if quote:
            current.append(char)
            if char == quote:
                if index + 1 < len(cleaned) and cleaned[index + 1] == quote:
                    current.append(cleaned[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
        elif char == "[":
            quote = "]"
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == ";" and depth == 0:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    current.append(text[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
        elif char == "[":
            quote = "]"
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == separator and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        body = value[1:-1]
        quote = value[0]
        return body.replace(quote + quote, quote)
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        return value[1:-1]
    return value


def _read_identifier(text: str, index: int) -> tuple[str, int]:
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return "", index
    char = text[index]
    if char in {"'", '"', "`"}:
        quote = char
        end = index + 1
        while end < len(text):
            if text[end] == quote:
                if end + 1 < len(text) and text[end + 1] == quote:
                    end += 2
                    continue
                return text[index + 1 : end], end + 1
            end += 1
        return text[index + 1 :], len(text)
    if char == "[":
        end = text.find("]", index + 1)
        if end < 0:
            return text[index + 1 :], len(text)
        return text[index + 1 : end], end + 1
    match = _IDENT_RE.match(text, index)
    if not match:
        return "", index
    return match.group(0), match.end()


def _read_identifier_list(text: str) -> list[str]:
    return [name for name in (_unquote(item) for item in _split_top_level(text)) if name]


def _normalize_type(raw: str) -> str:
    value = " ".join(raw.split())
    if re.match(r"^ENUM\s*\(", value, re.IGNORECASE):
        return "ENUM" + value[value.find("(") :]
    return value.upper()


def _parse_enum_values(raw: str) -> list[Any]:
    inner = raw.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    values: list[Any] = []
    for item in _split_top_level(inner):
        item = item.strip()
        if not item:
            continue
        values.append(_unquote(item))
    return values


def _parse_default(text: str, index: int) -> tuple[Any, int]:
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None, index
    if text[index] in {"'", '"'}:
        quote = text[index]
        end = index + 1
        while end < len(text):
            if text[end] == quote:
                if end + 1 < len(text) and text[end + 1] == quote:
                    end += 2
                    continue
                return text[index + 1 : end], end + 1
            end += 1
        return text[index + 1 :], len(text)
    if text[index] == "(":
        depth = 0
        end = index
        while end < len(text):
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
                if depth == 0:
                    return text[index : end + 1], end + 1
            end += 1
        return text[index:], len(text)
    match = re.match(r"[^\s,]+", text[index:])
    if not match:
        return None, index
    token = match.group(0)
    return token, index + len(token)


def _find_keyword(text: str, keywords: set[str], start: int = 0) -> int:
    """在括号深度 0 找到第一个独立关键字。"""
    depth = 0
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "[":
            quote = "]"
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and (char.isalpha() or char == "_"):
            match = _IDENT_RE.match(text, index)
            if match:
                word = match.group(0).upper()
                if word in keywords:
                    return index
                index = match.end()
                continue
        index += 1
    return -1


def _extract_parens(text: str, start: int = 0) -> tuple[str, int, int]:
    """返回 (括号内文本, 左括号位置, 右括号位置)。"""
    left = text.find("(", start)
    if left < 0:
        return "", -1, -1
    depth = 0
    quote: str | None = None
    index = left
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[left + 1 : index], left, index
        index += 1
    return "", left, -1


def _read_keyword_value(text: str, keyword: str) -> str | None:
    pattern = re.compile(rf"\b{keyword}\b\s*(?:=\s*)?(.+)$", re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    value = value.rstrip(",").strip()
    return _unquote(value)


def _parse_column(part: str) -> ParsedColumn | None:
    text = re.sub(r"^CONSTRAINT\s+\S+\s+", "", part.strip(), flags=re.IGNORECASE)
    name, index = _read_identifier(text, 0)
    if not name:
        return None
    remainder = text[index:].strip()
    if re.match(r"^(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\b", remainder, re.IGNORECASE):
        return None
    stop = _find_keyword(remainder, _CONSTRAINT_KEYWORDS)
    raw_type = remainder if stop < 0 else remainder[:stop]
    type_text = _normalize_type(raw_type).rstrip(",")
    column = ParsedColumn(name=name, type=type_text)
    upper = remainder.upper()
    column.nullable = not bool(re.search(r"\bNOT\s+NULL\b", upper))
    if re.search(r"\bPRIMARY\s+KEY\b", upper):
        column.primary_key = True
        column.nullable = False
    if re.search(r"\bUNIQUE\b", upper):
        column.unique = True
    default_match = re.search(r"\bDEFAULT\b", remainder, re.IGNORECASE)
    if default_match:
        value, _ = _parse_default(remainder, default_match.end())
        column.default = value
    comment = _read_keyword_value(remainder, "COMMENT")
    if comment:
        column.comment = comment[:1_000]
    enum_match = _ENUM_RE.match(type_text)
    if enum_match:
        column.enum_values = _parse_enum_values(enum_match.group(1))
    check_match = _CHECK_IN_RE.search(remainder.strip().rstrip(","))
    if check_match and not column.enum_values:
        column.enum_values = _parse_enum_values(check_match.group(1))
    return column


def _parse_pk_columns(part: str) -> list[str]:
    inner, _, _ = _extract_parens(part)
    return _read_identifier_list(inner)


def _parse_references(rest: str) -> tuple[str, list[str]]:
    match = re.search(r"\bREFERENCES\b", rest, re.IGNORECASE)
    if not match:
        return "", []
    parent, index = _read_identifier(rest, match.end())
    inner, _, _ = _extract_parens(rest, index)
    return parent, _read_identifier_list(inner)


def _parse_table_constraint(
    part: str, table: ParsedTable
) -> bool:
    text = re.sub(r"^CONSTRAINT\s+\S+\s+", "", part.strip(), flags=re.IGNORECASE)
    upper = text.upper()
    if upper.startswith("PRIMARY KEY"):
        table.primary_key = _parse_pk_columns(text)
        for name in table.primary_key:
            column = table.column_index().get(name)
            if column is not None:
                column.primary_key = True
                column.nullable = False
        return True
    if upper.startswith("UNIQUE"):
        for name in _parse_pk_columns(text):
            column = table.column_index().get(name)
            if column is not None:
                column.unique = True
        return True
    if upper.startswith("FOREIGN KEY"):
        child_inner, _, closed = _extract_parens(text)
        if closed < 0:
            return True
        parent, parent_columns = _parse_references(text[closed + 1 :])
        child_columns = _read_identifier_list(child_inner)
        if child_columns and parent and parent_columns:
            table.foreign_keys.append(
                ParsedForeignKey(
                    child_table=table.name,
                    child_columns=child_columns,
                    parent_table=parent,
                    parent_columns=parent_columns,
                )
            )
        return True
    if upper.startswith("CHECK"):
        check_match = _CHECK_IN_RE.search(text.rstrip(","))
        if check_match:
            values = _parse_enum_values(check_match.group(1))
            name_match = re.search(
                r"CHECK\s*\(\s*[\"`\[]?([A-Za-z_][\w$]*)", text, re.IGNORECASE
            )
            if name_match:
                column = table.column_index().get(name_match.group(1))
                if column is not None and not column.enum_values:
                    column.enum_values = values
        return True
    return False


def _parse_create_table(statement: str, warnings: list[str]) -> ParsedTable | None:
    match = re.search(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\b", statement, re.IGNORECASE)
    if not match:
        return None
    rest = statement[match.end() :].lstrip()
    rest = re.sub(r"^IF\s+NOT\s+EXISTS\s+", "", rest, flags=re.IGNORECASE)
    name, index = _read_identifier(rest, 0)
    if not name:
        warnings.append("create_table_name_missing")
        return None
    inner, _, closed = _extract_parens(rest, index)
    if closed < 0:
        warnings.append(f"create_table_unclosed:{name}")
        return None
    table = ParsedTable(name=name)
    table.comment = _read_keyword_value(rest[closed + 1 :], "COMMENT") or ""
    for part in _split_top_level(inner):
        if _parse_table_constraint(part, table):
            continue
        column = _parse_column(part)
        if column is None:
            warnings.append(f"column_unparsed:{name}:{part[:60]}")
            continue
        table.columns.append(column)
        if re.search(r"\bREFERENCES\b", part, re.IGNORECASE):
            _inline_reference(column, part, table)
    if not table.primary_key:
        table.primary_key = [
            column.name for column in table.columns if column.primary_key
        ]
    if len(table.columns) > MAX_COLUMNS_PER_TABLE:
        raise DDLError(
            f"{name} 列数 {len(table.columns)} 超过上限 {MAX_COLUMNS_PER_TABLE}"
        )
    return table


def _inline_reference(column: ParsedColumn, part: str, table: ParsedTable) -> None:
    rest = part[part.upper().find("REFERENCES") :] if "REFERENCES" in part.upper() else ""
    parent, parent_columns = _parse_references(rest)
    if parent and parent_columns:
        table.foreign_keys.append(
            ParsedForeignKey(
                child_table=table.name,
                child_columns=[column.name],
                parent_table=parent,
                parent_columns=parent_columns,
            )
        )


def _parse_alter_table(
    statement: str, tables: dict[str, ParsedTable], warnings: list[str]
) -> None:
    match = re.search(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?", statement, re.IGNORECASE)
    if not match:
        return
    name, index = _read_identifier(statement, match.end())
    table = tables.get(name)
    if table is None:
        warnings.append(f"alter_table_unknown:{name}")
        return
    rest = statement[index:].strip()
    rest = re.sub(r"^ADD\s+", "", rest, flags=re.IGNORECASE)
    if re.match(r"^(CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\b", rest, re.IGNORECASE):
        _parse_table_constraint(rest, table)
        return
    if re.match(r"^COLUMN\b", rest, re.IGNORECASE):
        rest = re.sub(r"^COLUMN\s+", "", rest, flags=re.IGNORECASE)
    if re.match(r"^(PRIMARY\s+KEY|UNIQUE|CHECK)\b", rest, re.IGNORECASE):
        _parse_table_constraint(rest, table)
        return
    column = _parse_column(rest)
    if column is not None:
        table.columns.append(column)
        if column.primary_key and column.name not in table.primary_key:
            table.primary_key.append(column.name)
        _inline_reference(column, rest, table)
        return
    warnings.append(f"alter_table_unparsed:{name}:{rest[:60]}")


def parse_ddl(text: str, *, dialect: str = "sqlite") -> ParsedDDL:
    if len(text.encode("utf-8")) > MAX_DDL_BYTES:
        raise DDLError(f"DDL 过大: {len(text.encode('utf-8'))} bytes")
    result = ParsedDDL()
    tables: dict[str, ParsedTable] = {}
    for statement in split_statements(text):
        head = statement.lstrip().upper()
        if head.startswith("CREATE"):
            if "TABLE" not in head:
                result.warnings.append(f"statement_unsupported:{head[:40]}")
                continue
            table = _parse_create_table(statement, result.warnings)
            if table is None:
                continue
            if table.name in tables:
                result.warnings.append(f"table_duplicate:{table.name}")
                continue
            tables[table.name] = table
            if len(tables) > MAX_TABLES:
                raise DDLError(f"表数量超过上限 {MAX_TABLES}")
            continue
        if head.startswith("ALTER"):
            _parse_alter_table(statement, tables, result.warnings)
            continue
        if head.startswith(("PRAGMA", "SET", "BEGIN", "COMMIT", "INSERT")):
            continue
        result.warnings.append(f"statement_unsupported:{head[:40]}")
    for table in tables.values():
        unique: list[ParsedForeignKey] = []
        seen: set[tuple[Any, ...]] = set()
        for fk in table.foreign_keys:
            key = (
                fk.child_table,
                tuple(fk.child_columns),
                fk.parent_table,
                tuple(fk.parent_columns),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(fk)
        table.foreign_keys = unique
    result.tables = list(tables.values())
    return result


__all__ = [
    "DDLError",
    "MAX_DDL_BYTES",
    "ParsedColumn",
    "ParsedDDL",
    "ParsedForeignKey",
    "ParsedTable",
    "parse_ddl",
    "split_statements",
]
