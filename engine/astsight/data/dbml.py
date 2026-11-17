"""DBML → ParsedDDL（C5a 的另一种结构事实源）。

覆盖常见写法：`Table` / `Enum` / `Ref:` / 行内 `[ref: > t.c]` / `indexes`
里的单列唯一索引 / `Note:` 注释。DBML 不是 SQL，解析器只提取结构事实；
无法识别的写法进 warnings，不猜。
"""
from __future__ import annotations

import re
from typing import Any

from .ddl import (
    MAX_COLUMNS_PER_TABLE,
    MAX_TABLES,
    ParsedColumn,
    ParsedDDL,
    ParsedForeignKey,
    ParsedTable,
    normalize_type,
    parse_enum_values,
)

MAX_DBML_BYTES = 5 * 1024 * 1024

_REGEX_KEYWORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENUM_TYPE_RE = re.compile(r"^(?:ENUM|SET)\s*\((.*)\)$", re.IGNORECASE | re.DOTALL)


class DBMLError(ValueError):
    """DBML 不可解析或超出资源上限。"""


def parse_dbml(text: str) -> ParsedDDL:
    if len(text.encode("utf-8")) > MAX_DBML_BYTES:
        raise DBMLError(f"DBML 过大: {len(text.encode('utf-8'))} bytes")
    cleaned = _strip_comments(text)
    result = ParsedDDL()
    aliases: dict[str, str] = {}

    # 1) 先收集 Enum，列类型可能引用文件末尾定义的枚举。
    enums: dict[str, list[Any]] = {}
    raw_tables: list[tuple[str, str]] = []
    raw_refs: list[str] = []
    cursor = 0
    while cursor < len(cleaned):
        start = _skip_whitespace(cleaned, cursor)
        if start >= len(cleaned):
            break
        match = _REGEX_KEYWORD.match(cleaned, start)
        if match is None:
            cursor = _skip_line(cleaned, start)
            continue
        keyword = match.group(0).lower()
        if keyword in {"table", "enum", "project", "tablegroup"}:
            brace = cleaned.find("{", match.end())
            if brace < 0:
                result.warnings.append(f"dbml_block_unclosed:{keyword}")
                break
            end = _match_brace(cleaned, brace)
            if end < 0:
                raise DBMLError(f"DBML {keyword} 块缺少右花括号")
            header = cleaned[match.end() : brace]
            body = cleaned[brace + 1 : end]
            if keyword == "table":
                raw_tables.append((header, body))
            elif keyword == "enum":
                name = _table_name(header)
                if not name:
                    result.warnings.append("dbml_enum_name_missing")
                else:
                    enums[name.lower()] = _enum_values(body)
            cursor = end + 1
            continue
        if keyword == "note":
            cursor = _skip_line(cleaned, start)
            continue
        if keyword == "ref":
            line_end = cleaned.find("\n", start)
            line = cleaned[start : line_end if line_end >= 0 else len(cleaned)]
            if "{" in line:
                brace = cleaned.find("{", start)
                end = _match_brace(cleaned, brace)
                if end < 0:
                    raise DBMLError("DBML Ref 块缺少右花括号")
                result.warnings.append("dbml_ref_block_unsupported")
                cursor = end + 1
                continue
            raw_refs.append(line.strip())
            cursor = line_end if line_end >= 0 else len(cleaned)
            continue
        cursor = _skip_line(cleaned, start)

    # 2) 解析表。
    tables: dict[str, ParsedTable] = {}
    inline_refs: list[tuple[str, str, str]] = []
    for header, body in raw_tables:
        header_text, _settings = _pop_settings(header)
        name, alias = _table_name_and_alias(header_text)
        if not name:
            result.warnings.append("dbml_table_name_missing")
            continue
        if name in tables:
            result.warnings.append(f"table_duplicate:{name}")
            continue
        table, table_refs = _parse_table(name, body, enums, result.warnings)
        tables[name] = table
        inline_refs.extend(
            (name, column, value) for column, value in table_refs
        )
        if alias:
            aliases[alias.lower()] = name
        if len(tables) > MAX_TABLES:
            raise DBMLError(f"表数量超过上限 {MAX_TABLES}")

    # 3) 解析行内与顶层 Ref（此时别名已全部收集）。
    for table_name, column_name, value in inline_refs:
        _apply_inline_ref(
            table_name, column_name, value, tables, aliases, result.warnings
        )
    for line in raw_refs:
        _apply_ref(line, tables, aliases, result.warnings)

    result.tables = list(tables.values())
    _dedupe_foreign_keys(result.tables)
    return result


def _parse_table(
    name: str,
    body: str,
    enums: dict[str, list[Any]],
    warnings: list[str],
) -> tuple[ParsedTable, list[tuple[str, str]]]:
    table = ParsedTable(name=name)
    body, indexes_body = _split_indexes(body, warnings)
    inline_refs: list[tuple[str, str]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        note = re.match(r"(?i)^note\s*:\s*(.+)$", line)
        if note:
            table.comment = _literal(note.group(1)) or ""
            continue
        text, groups = _pop_settings(line)
        if not text:
            continue
        settings_text = ", ".join(groups)
        column = _parse_column(text, settings_text, enums)
        if column is None:
            warnings.append(f"dbml_column_unparsed:{name}:{line[:60]}")
            continue
        table.columns.append(column)
        if column.primary_key and column.name not in table.primary_key:
            table.primary_key.append(column.name)
        for setting in _split_settings(settings_text):
            key, _, value = setting.partition(":")
            if key.strip().lower() == "ref" and value.strip():
                inline_refs.append((column.name, value.strip()))
    if len(table.columns) > MAX_COLUMNS_PER_TABLE:
        raise DBMLError(
            f"{name} 列数 {len(table.columns)} 超过上限 {MAX_COLUMNS_PER_TABLE}"
        )
    if indexes_body:
        _apply_indexes(table, indexes_body, warnings)
    return table, inline_refs


def _parse_column(
    text: str, settings_text: str, enums: dict[str, list[Any]]
) -> ParsedColumn | None:
    name, index = _read_token(text, 0)
    if not name:
        return None
    raw_type = text[index:].strip()
    if not raw_type:
        return None
    type_text = _resolve_enum_type(normalize_type(raw_type), enums)
    column = ParsedColumn(name=name, type=type_text)
    column.enum_values = _type_enum_values(type_text)
    for setting in _split_settings(settings_text):
        key, _, value = setting.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in {"pk", "primary key"}:
            column.primary_key = True
            column.nullable = False
        elif key == "not null":
            column.nullable = False
        elif key == "null":
            column.nullable = True
        elif key == "unique":
            column.unique = True
        elif key == "increment":
            continue
        elif key == "default":
            column.default = _literal(value)
        elif key == "note":
            column.comment = (_literal(value) or "")[:1_000]
        elif key == "ref":
            # 行内引用在表解析完成后统一处理。
            continue
    return column


def _apply_indexes(
    table: ParsedTable, body: str, warnings: list[str]
) -> None:
    columns = table.column_index()
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        text, groups = _pop_settings(line)
        if not text:
            continue
        names = (
            [item.strip() for item in text.strip("()").split(",")]
            if text.startswith("(")
            else [text]
        )
        names = [
            name for name in (_unquote_token(item) for item in names) if name
        ]
        if not names:
            continue
        settings = _split_settings(", ".join(groups))
        if any(part.strip().lower() == "pk" for part in settings):
            table.primary_key = names
            for name in names:
                column = columns.get(name)
                if column is not None:
                    column.primary_key = True
                    column.nullable = False
            continue
        unique = any(part.strip().lower() == "unique" for part in settings)
        if not unique:
            continue
        if len(names) == 1:
            column = columns.get(names[0])
            if column is not None:
                column.unique = True
        else:
            warnings.append(
                f"dbml_unique_composite_ignored:{table.name}:{','.join(names)}"
            )


def _apply_ref(
    line: str,
    tables: dict[str, ParsedTable],
    aliases: dict[str, str],
    warnings: list[str],
) -> None:
    match = re.match(
        r"(?is)^ref\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*)?:\s*(.+)$", line
    )
    if match is None:
        warnings.append(f"dbml_ref_unparsed:{line[:60]}")
        return
    left, operator, right = _split_ref(match.group(1))
    if not operator:
        warnings.append(f"dbml_ref_unparsed:{line[:60]}")
        return
    if operator == "<":
        child, parent = right, left
    else:
        child, parent = left, right
    child_table, child_columns = _ref_side(child)
    parent_table, parent_columns = _ref_side(parent)
    child_table = aliases.get(child_table.lower(), child_table)
    parent_table = aliases.get(parent_table.lower(), parent_table)
    _add_fk(
        child_table,
        child_columns,
        parent_table,
        parent_columns,
        tables,
        warnings,
    )


def _split_ref(text: str) -> tuple[str, str, str]:
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            continue
        if depth == 0 and char in {"<", ">", "-"}:
            return text[:index].strip(), char, text[index + 1 :].strip()
    return text.strip(), "", ""


def _ref_side(text: str) -> tuple[str, list[str]]:
    value = (text or "").strip()
    if "(" in value and value.endswith(")"):
        head, _, rest = value.partition("(")
        table = _ref_table(head)
        columns = [
            _unquote_token(item.strip())
            for item in rest[:-1].split(",")
            if item.strip()
        ]
        return table, columns
    table, _, column = value.rpartition(".")
    return _ref_table(table), ([_unquote_token(column)] if column else [])


def _ref_table(name: str) -> str:
    parts = [
        _unquote_token(part)
        for part in name.strip().split(".")
    ]
    return ".".join(part for part in parts if part)


def _split_indexes(body: str, warnings: list[str]) -> tuple[str, str]:
    """把 ``indexes { ... }`` 从表体里摘出去，避免索引行被当成列。"""
    match = re.search(r"(?im)^\s*indexes\s*\{", body)
    if match is None:
        return body, ""
    brace = body.find("{", match.start())
    end = _match_brace(body, brace)
    if end < 0:
        warnings.append("dbml_indexes_unclosed")
        return body, ""
    remaining = body[: match.start()] + "\n" + body[end + 1 :]
    return remaining, body[brace + 1 : end]


def _apply_inline_ref(
    table_name: str,
    column_name: str,
    value: str,
    tables: dict[str, ParsedTable],
    aliases: dict[str, str],
    warnings: list[str],
) -> None:
    match = re.match(r"^\s*([<>-])\s*(.+)$", value)
    if match is None:
        warnings.append(f"dbml_inline_ref_unparsed:{table_name}.{column_name}")
        return
    operator, side = match.group(1), match.group(2)
    other_table, other_columns = _ref_side(side)
    other_table = aliases.get(other_table.lower(), other_table)
    if operator == "<":
        # 本列是被引用端，目标表才是子表。
        child_table = other_table
        child_columns = other_columns
        parent_table = table_name
        parent_columns = [column_name]
    else:
        child_table = table_name
        child_columns = [column_name]
        parent_table = other_table
        parent_columns = other_columns
    _add_fk(
        child_table,
        child_columns,
        parent_table,
        parent_columns,
        tables,
        warnings,
    )


def _add_fk(
    child_table: str,
    child_columns: list[str],
    parent_table: str,
    parent_columns: list[str],
    tables: dict[str, ParsedTable],
    warnings: list[str],
) -> None:
    if not child_table or not parent_table or not child_columns or not parent_columns:
        warnings.append(f"dbml_ref_unparsed:{child_table}->{parent_table}")
        return
    child_spec = tables.get(child_table)
    parent_spec = tables.get(parent_table)
    if child_spec is None or parent_spec is None:
        warnings.append(f"dbml_ref_unknown_table:{child_table}->{parent_table}")
        return
    child_index = child_spec.column_index()
    parent_index = parent_spec.column_index()
    if any(name not in child_index for name in child_columns) or any(
        name not in parent_index for name in parent_columns
    ):
        warnings.append(f"dbml_ref_unknown_column:{child_table}->{parent_table}")
        return
    child_spec.foreign_keys.append(
        ParsedForeignKey(
            child_table=child_table,
            child_columns=child_columns,
            parent_table=parent_table,
            parent_columns=parent_columns,
        )
    )


def _dedupe_foreign_keys(tables: list[ParsedTable]) -> None:
    for table in tables:
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


def _resolve_enum_type(
    type_text: str, enums: dict[str, list[Any]]
) -> str:
    if _ENUM_TYPE_RE.match(type_text):
        return type_text
    key = type_text.lower()
    values = enums.get(key)
    if values is None and "." in key:
        values = enums.get(key.rsplit(".", 1)[-1])
    if values is None:
        return type_text
    body = ", ".join(_sql_quote(value) for value in values)
    return f"ENUM({body})"


def _type_enum_values(type_text: str) -> list[Any]:
    match = _ENUM_TYPE_RE.match(type_text)
    if not match:
        return []
    return parse_enum_values(match.group(1))


def _enum_values(body: str) -> list[Any]:
    values: list[Any] = []
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        text, _groups = _pop_settings(line)
        value = _literal(text)
        if value in (None, ""):
            continue
        values.append(value)
    return values


def _table_name(header: str) -> str:
    name, _alias = _table_name_and_alias(header)
    return name


def _table_name_and_alias(header: str) -> tuple[str, str]:
    text, _settings = _pop_settings(header)
    match = re.match(r"(?is)^\s*([^\s]+)(?:\s+as\s+([A-Za-z_][\w]*))?\s*$", text)
    if match is None:
        return "", ""
    return _ref_table(match.group(1)), (match.group(2) or "")


def _pop_settings(text: str) -> tuple[str, list[str]]:
    """移除所有顶层 ``[...]`` 设置块，返回剩余文本与设置内容。"""
    groups: list[str] = []
    out: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "[":
            end = _match_bracket(text, index)
            if end < 0:
                break
            groups.append(text[index + 1 : end])
            index = end + 1
            continue
        out.append(char)
        index += 1
    return "".join(out).strip(), groups


def _split_settings(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
            continue
        if char in "({":
            depth += 1
        elif char in ")}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _read_token(text: str, index: int) -> tuple[str, int]:
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return "", index
    char = text[index]
    if char in {"'", '"', "`"}:
        end = text.find(char, index + 1)
        if end < 0:
            return text[index + 1 :], len(text)
        return text[index + 1 : end], end + 1
    match = re.match(r"[^\s(]+", text[index:])
    if match is None:
        return "", index
    return match.group(0), index + len(match.group(0))


def _unquote_token(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        return value[1:-1]
    return value


def _literal(value: str) -> Any:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.lower() == "null":
        return None
    if raw.startswith("`") and raw.endswith("`") and len(raw) >= 2:
        return raw[1:-1]
    if raw[0] in {"'", '"'} and raw[-1] == raw[0] and len(raw) >= 2:
        return raw[1:-1].replace(raw[0] * 2, raw[0])
    return raw


def _sql_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _strip_comments(text: str) -> str:
    out: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    out.append(text[index + 1])
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and text.startswith("//", index):
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        if char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _skip_line(text: str, index: int) -> int:
    end = text.find("\n", index)
    return len(text) if end < 0 else end + 1


def _match_brace(text: str, start: int) -> int:
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
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _match_bracket(text: str, start: int) -> int:
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
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


__all__ = ["DBMLError", "MAX_DBML_BYTES", "parse_dbml"]
