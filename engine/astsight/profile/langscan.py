"""Java / Go / Node 下游 HTTP 调用点的轻量静态扫描（C3 P1）。

与 `downstream.py` 的 Python AST 扫描并列：这里不 import、不执行被测项目，
只用带边界上限的文本扫描识别常见出站 HTTP 客户端。目标不是完整语言前端，
而是把"能静态证明"的调用点交给 `build_dep_id_plan`；证明不了的一律
`unsupported`，不猜。

识别范围（v1）：

- Java/Kotlin：`RestTemplate`、`WebClient`、Java 11 `HttpClient`、OkHttp、
  Apache HttpClient；URL 支持字面量、`+` 拼接、`URI.create`、`@Value("${...}")`
  字段、`System.getenv` / `System.getProperty` / `Environment.getProperty`。
- Go：`http.Get/Post/NewRequest(WithContext)`、`http.Client` / resty 实例方法；
  URL 支持字面量、`+` 拼接、`fmt.Sprintf`、`os.Getenv` / `viper.GetString`。
- Node/TS：全局 `fetch`、`axios`（含 `axios.create` 实例）、`got`、
  `http.request` / `https.request`；URL 支持字面量、模板字符串、`+` 拼接、
  `process.env`。

显式不做：声明式客户端（Java Feign、Go 代码生成 client、TS SDK）、动态代理、
注册中心、HTTPS 目标。落在这些形态上的调用不会产生路由，由画像层的
`downstream_unmapped` / `downstream_unsupported` 显式暴露。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from ..mock.identity import normalize_method
from .callsite import HttpCallSite

LANGUAGE_BY_SUFFIX = {
    ".java": "java",
    ".kt": "java",
    ".kts": "java",
    ".go": "go",
    ".js": "node",
    ".jsx": "node",
    ".ts": "node",
    ".tsx": "node",
    ".mjs": "node",
    ".cjs": "node",
}

MAX_BLOCK_CHARS = 200_000
MAX_VARIABLES = 8
MAX_CALLEES = 50

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_JAVA_PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")
_JS_PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")
_FORMAT_PLACEHOLDER = re.compile(r"%(?:[-+0-9.#]*[sdvqf])|\{[^{}]*\}")
_NOT_CALLEES = {
    "if", "for", "while", "switch", "catch", "return", "new", "sizeof", "typeof",
    "function", "assert", "print", "println", "printf", "sprintf", "len", "make",
    "append", "panic", "error", "require", "import", "super", "this", "self",
}


@dataclass
class LangScan:
    sites: list[HttpCallSite] = field(default_factory=list)
    local_callees: list[str] = field(default_factory=list)
    unsupported_reasons: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UrlEval:
    kind: str  # target / path / config / dynamic
    target: str | None = None
    path: str | None = None
    config_key: str | None = None
    detail: str = ""

    def equivalent(self, other: "UrlEval") -> bool:
        return (
            self.kind == other.kind
            and self.target == other.target
            and self.path == other.path
            and self.config_key == other.config_key
        )


def _dynamic(detail: str = "", *, path: str | None = None) -> UrlEval:
    return UrlEval("dynamic", path=path, detail=detail)


def _literal(text: str) -> UrlEval:
    value = str(text or "").strip()
    if not value:
        return _dynamic("empty_url")
    if "://" in value:
        scheme = value.split("://", 1)[0].lower()
        if scheme in ("http", "https"):
            authority = value.split("://", 1)[1].split("/", 1)[0]
            if not authority or "{" in authority:
                return _dynamic("dynamic_host")
            return UrlEval("target", target=value)
        return _dynamic("unsupported_scheme")
    if value.startswith("/"):
        return UrlEval("path", path=value)
    return _dynamic("relative_url")


def _join_target(base: str, suffix: str) -> str:
    left = base.rstrip("/")
    right = suffix if suffix.startswith("/") else "/" + suffix
    return left + right


def _combine(left: UrlEval, right: UrlEval) -> UrlEval:
    if left.kind == "target":
        if right.kind == "path" and right.path:
            return UrlEval("target", target=_join_target(left.target or "", right.path))
        if right.kind == "dynamic" and right.path:
            return _dynamic(right.detail or "dynamic_suffix")
        return _dynamic("dynamic_suffix")
    if left.kind == "path":
        if right.kind == "path" and left.path and right.path:
            return UrlEval("path", path=left.path + right.path)
        return _dynamic("dynamic_suffix", path=left.path)
    if left.kind == "config":
        if right.kind == "path" and right.path:
            return UrlEval("config", path=right.path, config_key=left.config_key)
        if right.kind == "dynamic" and right.path:
            return UrlEval(
                "config", path=right.path, config_key=left.config_key, detail=right.detail
            )
        return _dynamic("dynamic_suffix", path=left.path)
    if left.kind == "dynamic":
        if left.detail == "dynamic_host" and right.kind == "path":
            return _dynamic("dynamic_host", path=right.path)
        if left.path:
            return _dynamic(left.detail or "dynamic_suffix", path=left.path)
        if right.kind == "path":
            return _dynamic(left.detail or "target_dynamic", path=right.path)
    return _dynamic(left.detail or right.detail or "target_dynamic")


# ───────────────────────── 文本工具 ─────────────────────────


def language_for(path: Path | str) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower())


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, max(0, index)) + 1


def _index_for_line(text: str, line: int) -> int:
    if line <= 1:
        return 0
    index = 0
    for _ in range(line - 1):
        found = text.find("\n", index)
        if found < 0:
            return len(text)
        index = found + 1
    return index


def _mask_comments(text: str) -> str:
    """把 //、/* */ 注释替换为空格，保留索引与字符串内容。"""
    chars = list(text)
    quote = ""
    escape = False
    i = 0
    while i < len(chars):
        ch = chars[i]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "/" and i + 1 < len(chars) and chars[i + 1] == "/":
            while i < len(chars) and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        if ch == "/" and i + 1 < len(chars) and chars[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = len(chars) if end < 0 else end + 2
            for j in range(i, end):
                if chars[j] != "\n":
                    chars[j] = " "
            i = end
            continue
        i += 1
    return "".join(chars)


def _split_top_level(text: str, separator: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    depth = 0
    quote = ""
    escape = False
    start = 0
    for index, ch in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == separator and depth == 0:
            spans.append((start, index))
            start = index + 1
    spans.append((start, len(text)))
    return spans


def _matching_brace(text: str, open_index: int) -> int:
    depth = 0
    quote = ""
    escape = False
    for index in range(open_index, len(text)):
        ch = text[index]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1


def _matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    quote = ""
    escape = False
    for index in range(open_index, len(text)):
        ch = text[index]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _call_args(text: str, paren_index: int) -> tuple[list[str], int]:
    end = _matching_paren(text, paren_index)
    if end is None:
        return [], paren_index
    inner = text[paren_index + 1 : end]
    spans = _split_top_level(inner, ",")
    # 过滤末尾空参数（尾随逗号）
    args = [inner[a:b].strip() for a, b in spans]
    args = [arg for arg in args if arg]
    return args, end


def _strip_expr(expr: str) -> str:
    value = str(expr or "").strip()
    while value.startswith("(") and value.endswith(")"):
        if _matching_paren(value, 0) != len(value) - 1:
            break
        value = value[1:-1].strip()
    # 常见类型转换/包装
    value = re.sub(r"^(?:final\s+|const\s+|let\s+|var\s+)", "", value).strip()
    wrappers = (
        r"(?:URI\.create|URI|new\s+URI|new\s+String|String|"
        r"encodeURI|encodeURIComponent|new\s+URL|URL|url\.Parse|"
        r"Path\.of|Paths\.get|Objects\.requireNonNull)"
    )
    match = re.match(rf"{wrappers}\s*\((.*)\)$", value, re.S)
    if match:
        return _strip_expr(match.group(1))
    for suffix in (".toString()", ".ToURL()", ".string()", ".TrimSpace()", ".strip()", ".trim()"):
        if value.endswith(suffix):
            return _strip_expr(value[: -len(suffix)])
    return value


def _quoted(expr: str) -> tuple[str, str] | None:
    value = expr.strip()
    if len(value) < 2:
        return None
    quote = value[0]
    if quote not in ("'", '"', "`") or value[-1] != quote:
        return None
    inner = value[1:-1]
    # 未转义的同类引号说明不是单个字符串字面量
    if re.search(rf"(?<!\\){re.escape(quote)}", inner):
        return None
    return quote, inner


def _spring_env_key(raw: str) -> str:
    value = str(raw or "").strip()
    if _ENV_KEY_RE.match(value):
        return value
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").upper()
    if not normalized:
        return value
    if not re.match(r"^[A-Za-z_]", normalized):
        normalized = "_" + normalized
    return normalized


def _unwrap_java_placeholder(expr: str) -> str | None:
    value = expr.strip()
    if value.startswith("${") and value.endswith("}"):
        return _spring_env_key(value[2:-1])
    if value.startswith("{") and value.endswith("}"):
        return _spring_env_key(value[1:-1])
    return None


# ───────────────────────── 表达式求值 ─────────────────────────


class _Evaluator:
    def __init__(self, language: str, variables: dict[str, list[str]]):
        self.language = language
        self.variables = variables

    def evaluate(self, expr: str | None, seen: frozenset[str] = frozenset()) -> UrlEval:
        value = _strip_expr(expr or "")
        if not value:
            return _dynamic("empty_url")

        config = self._config(value)
        if config is not None:
            return config

        quoted = _quoted(value)
        if quoted is not None:
            return self._string(quoted[0], quoted[1], seen)

        if re.fullmatch(r"[A-Za-z_$][\w$.:-]*", value):
            return self._variable(value, seen)

        if self.language == "java":
            formatted = self._java_format(value, seen)
            if formatted is not None:
                return formatted
            concat = self._concat(value, seen)
            if concat is not None:
                return concat
            if value.endswith(")") and ".concat(" in value:
                head, _, tail = value.rpartition(".concat(")
                inner = tail[:-1]
                return _combine(self.evaluate(head, seen), self.evaluate(inner, seen))
        else:
            formatted = self._sprintf(value, seen)
            if formatted is not None:
                return formatted
            concat = self._concat(value, seen)
            if concat is not None:
                return concat

        return _dynamic("unsupported_expression")

    # ---- 变量 ----

    def _variable(self, name: str, seen: frozenset[str]) -> UrlEval:
        clean = name.strip()
        if clean.startswith("this.") or clean.startswith("self."):
            clean = clean.split(".", 1)[1]
        if clean in seen:
            return _dynamic("cyclic_variable")
        values = self.variables.get(clean) or []
        if not values:
            return _dynamic("target_dynamic")
        results = [self.evaluate(item, seen | {clean}) for item in values[-MAX_VARIABLES:]]
        first = results[0]
        if all(first.equivalent(item) for item in results[1:]):
            return first
        return _dynamic("variable_reassigned")

    # ---- 字符串与模板 ----

    def _string(self, quote: str, inner: str, seen: frozenset[str]) -> UrlEval:
        if self.language == "java":
            if not _JAVA_PLACEHOLDER.search(inner):
                return _literal(inner)
            # Spring 占位符只接受 `${KEY}` 或 `${KEY}/path` 形式
            match = _JAVA_PLACEHOLDER.match(inner)
            if match is None:
                return _dynamic("unsupported_expression")
            key = _spring_env_key(match.group(1))
            rest = inner[match.end() :]
            if not rest:
                return UrlEval("config", config_key=key)
            if rest.startswith("/") and not _JAVA_PLACEHOLDER.search(rest):
                return UrlEval("config", path=rest, config_key=key)
            return _dynamic("unsupported_expression")
        if quote != "`":
            return _literal(inner)
        placeholder = _JS_PLACEHOLDER

        pieces: list[UrlEval] = []
        cursor = 0
        for match in placeholder.finditer(inner):
            if match.start() > cursor:
                pieces.append(_literal(inner[cursor : match.start()]))
            raw = match.group(1).strip()
            key = _spring_env_key(raw) if self.language == "java" else ""
            pieces.append(
                UrlEval("config", config_key=key)
                if key
                else self.evaluate(raw, seen)
            )
            cursor = match.end()
        if cursor < len(inner):
            pieces.append(_literal(inner[cursor:]))
        if not pieces:
            return _literal(inner)
        result = pieces[0]
        for piece in pieces[1:]:
            result = _combine(result, piece)
        return result

    # ---- 拼接与格式化 ----

    def _concat(self, value: str, seen: frozenset[str]) -> UrlEval | None:
        spans = _split_top_level(value, "+")
        if len(spans) <= 1:
            return None
        result: UrlEval | None = None
        for start, end in spans:
            piece = value[start:end].strip()
            if not piece:
                return None
            current = self.evaluate(piece, seen)
            result = current if result is None else _combine(result, current)
            if result.kind == "dynamic" and not result.path:
                return result
        return result

    def _java_format(self, value: str, seen: frozenset[str]) -> UrlEval | None:
        match = re.match(r"String\.format\s*\(", value)
        if not match:
            return None
        args, _ = _call_args(value, match.end() - 1)
        if len(args) < 2:
            return None
        quoted = _quoted(args[0])
        if quoted is None:
            return None
        return self._format_pieces(quoted[1], args[1:], seen)

    def _sprintf(self, value: str, seen: frozenset[str]) -> UrlEval | None:
        match = re.match(r"fmt\.Sprintf\s*\(", value)
        if not match:
            return None
        args, _ = _call_args(value, match.end() - 1)
        if len(args) < 2:
            return None
        quoted = _quoted(args[0])
        if quoted is None:
            return None
        return self._format_pieces(quoted[1], args[1:], seen)

    def _format_pieces(
        self, template: str, args: list[str], seen: frozenset[str]
    ) -> UrlEval:
        pieces: list[UrlEval] = []
        cursor = 0
        index = 0
        for match in _FORMAT_PLACEHOLDER.finditer(template):
            if match.start() > cursor:
                pieces.append(_literal(template[cursor : match.start()]))
            if index < len(args):
                pieces.append(self.evaluate(args[index], seen))
            else:
                pieces.append(_dynamic("format_arg_missing"))
            index += 1
            cursor = match.end()
        if cursor < len(template):
            pieces.append(_literal(template[cursor:]))
        if not pieces:
            return _literal(template)
        result = pieces[0]
        for piece in pieces[1:]:
            result = _combine(result, piece)
        return result

    # ---- 配置键 ----

    def _config(self, value: str) -> UrlEval | None:
        placeholder = _unwrap_java_placeholder(value)
        if placeholder:
            return UrlEval("config", config_key=placeholder)

        if self.language == "java":
            match = re.fullmatch(
                r"(?:System\.getenv|System\.getProperty|"
                r"[\w.]+\.getProperty|[\w.]+\.getEnv)\s*\(\s*"
                r"(['\"])([^'\"]+)\1\s*\)",
                value,
                re.S,
            )
            if match:
                return UrlEval("config", config_key=_spring_env_key(match.group(2)))
        elif self.language == "go":
            match = re.fullmatch(
                r"(?:os\.(?:Getenv|LookupEnv)|getenv|"
                r"(?:viper|config|cfg)\.GetString)\s*\(\s*"
                r"(['\"`])([^'\"`]+)\1\s*\)",
                value,
                re.S,
            )
            if match:
                key = match.group(2)
                if re.search(r"(?:viper|config|cfg)\.GetString", value):
                    key = _spring_env_key(key)
                return UrlEval("config", config_key=key)
        else:
            match = re.fullmatch(
                r"process\.env(?:\.([A-Za-z_][\w]*)|\[\s*['\"]([^'\"]+)['\"]\s*\])",
                value,
                re.S,
            )
            if match:
                return UrlEval("config", config_key=match.group(1) or match.group(2))
            match = re.fullmatch(
                r"import\.meta\.env(?:\.([A-Za-z_][\w]*)|\[\s*['\"]([^'\"]+)['\"]\s*\])",
                value,
                re.S,
            )
            if match:
                return UrlEval("config", config_key=match.group(1) or match.group(2))
        return None


@dataclass
class _Hit:
    index: int
    method: str
    url_expr: str | None
    client: str


def _literal_method(value: str | None) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(?:HttpMethod|http\.Method|RequestMethod)\.([A-Za-z]+)", raw)
    if match:
        return normalize_method(match.group(1))
    quoted = _quoted(raw)
    if quoted is not None:
        return normalize_method(quoted[1])
    return ""


def _option_string(text: str, name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}\s*[:=]\s*(['\"`])([^'\"`]+)\1", text, re.S
    )
    return match.group(2) if match else None


# ───────────────────────── 变量绑定 ─────────────────────────


def _clean_rhs(value: str) -> str:
    text = str(value or "").strip()
    text = text.split("\n//", 1)[0]
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _add_binding(target: dict[str, list[str]], name: str, expr: str) -> None:
    if not name or not expr:
        return
    target.setdefault(name, []).append(_clean_rhs(expr))


def _collect_bindings(text: str, language: str) -> dict[str, list[str]]:
    variables: dict[str, list[str]] = {}
    if language == "java":
        # @Value("${KEY}") 字段：注解可能在上方多行
        for match in re.finditer(r"@Value\s*\(\s*(['\"])([^'\"]+)\1\s*\)", text):
            tail = text[match.end() : match.end() + 200]
            field = re.search(r"\b(?:String|URI|URL|var)\s+(\w+)", tail)
            if field:
                _add_binding(variables, field.group(1), f'"{match.group(2)}"')
        for match in re.finditer(
            r"(?:^|[;{}])\s*(?:(?:private|protected|public|static|final|"
            r"readonly|lateinit)\s+)*"
            r"(?:String|URI|URL|var)\s+(\w+)\s*=\s*([^;{{}}]+);",
            text,
            re.M,
        ):
            _add_binding(variables, match.group(1), match.group(2))
    elif language == "go":
        for match in re.finditer(
            r"(?m)^\s*(?:var|const)?\s*(\w+)\s*(?::=|=)\s*(.+)$", text
        ):
            _add_binding(variables, match.group(1), match.group(2))
        for match in re.finditer(
            r"(?m)^\s*(\w+)\s*:=\s*(.+)$", text
        ):
            _add_binding(variables, match.group(1), match.group(2))
    else:
        for match in re.finditer(
            r"(?:^|[;{}])\s*(?:const|let|var)\s+(\w+)\s*"
            r"(?::[^=;\n]+)?=\s*([^;\n]+)",
            text,
            re.M,
        ):
            _add_binding(variables, match.group(1), match.group(2))
        for match in re.finditer(
            r"(?:^|[;{}])\s*(?:(?:private|protected|public|readonly|static)\s+)*"
            r"(\w+)\s*=\s*([^;{}\n]+)",
            text,
            re.M,
        ):
            _add_binding(variables, match.group(1), match.group(2))
    return variables


# ───────────────────────── 函数块定位 ─────────────────────────


def _named_block(
    text: str, name: str, language: str, line_hint: int
) -> tuple[int, int, int, bool] | None:
    clean = re.split(r"[.#:]", str(name or "").strip())[-1]
    if not clean:
        return None
    escaped = re.escape(clean)
    if language == "java":
        pattern = rf"\b{escaped}\s*\([^;{{}}]*\)\s*(?:throws[^{{]+)?\{{"
    elif language == "go":
        pattern = rf"\bfunc\s+(?:\([^)]*\)\s*)?{escaped}\s*\([^)]*\)[^{{]*\{{"
    else:
        pattern = (
            rf"(?:\bfunction\s+{escaped}\s*\([^)]*\)\s*\{{|"
            rf"\b(?:const|let|var)\s+{escaped}\s*=\s*(?:async\s*)?"
            rf"(?:\([^)]*\)|\w+)\s*=>\s*\{{|"
            rf"\b(?:async\s+)?{escaped}\s*\([^)]*\)\s*(?::[^{{]+)?\{{|"
            rf"\b{escaped}\s*:\s*(?:async\s*)?(?:function\s*)?"
            rf"\([^)]*\)\s*=>\s*\{{)"
        )
    candidates: list[tuple[int, int, int, bool]] = []
    position = _index_for_line(text, line_hint) if line_hint > 0 else 0
    for match in re.finditer(pattern, text):
        brace = text.find("{", match.end() - 1)
        if brace < 0:
            continue
        end = _matching_brace(text, brace)
        decl_line = _line_of(text, match.start())
        contains = line_hint <= 0 or (
            match.start() <= position <= end if line_hint > 0 else True
        )
        candidates.append((brace + 1, end, decl_line, contains))
    if not candidates:
        return None
    # 行号命中优先；其次取声明行离 hint 最近的方法
    containing = [item for item in candidates if item[3]]
    pool = containing or candidates
    pool.sort(key=lambda item: (item[1] - item[0]))
    start, end, decl_line, _contains = pool[0]
    return start, end, decl_line


def _enclosing_block(text: str, line_hint: int) -> tuple[int, int, int] | None:
    if line_hint <= 0:
        return None
    position = _index_for_line(text, line_hint)
    stack: list[int] = []
    quote = ""
    escape = False
    for index, ch in enumerate(text[:position]):
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            stack.append(index)
        elif ch == "}" and stack:
            stack.pop()
    if not stack:
        return None
    start = stack[-1]
    end = _matching_brace(text, start)
    return start + 1, end, _line_of(text, start)


def _function_names(text: str, language: str, limit: int = 200) -> set[str]:
    names: set[str] = set()
    if language == "java":
        pattern = re.compile(
            r"(?m)^\s*(?:(?:public|private|protected|static|final|synchronized|"
            r"abstract|default)\s+)+[\w<>\[\],.?\s]+\s+(\w+)\s*\([^;{}]*\)\s*"
            r"(?:throws[^{]+)?\{"
        )
    elif language == "go":
        pattern = re.compile(r"func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(")
    else:
        pattern = re.compile(
            r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*"
            r"(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>|"
            r"(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{)"
        )
    for match in pattern.finditer(text):
        name = next((group for group in match.groups() if group), "")
        if name and name not in _NOT_CALLEES:
            names.add(name)
        if len(names) >= limit:
            break
    return names


def _block_callees(block: str, language: str, file_functions: set[str]) -> list[str]:
    calls = {
        match.group(1)
        for match in re.finditer(r"\b(\w+)\s*\(", block)
        if match.group(1) in file_functions and match.group(1) not in _NOT_CALLEES
    }
    return sorted(calls)[:MAX_CALLEES]


# ───────────────────────── 客户端识别 ─────────────────────────


_REST_ACTIONS = {
    "getForObject": "GET",
    "getForEntity": "GET",
    "postForObject": "POST",
    "postForEntity": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patchForObject": "PATCH",
    "headForHeaders": "HEAD",
    "optionsForAllow": "OPTIONS",
}


def _java_hits(block: str) -> list[_Hit]:
    hits: list[_Hit] = []

    for match in re.finditer(
        r"\b(\w+)\s*\.\s*(getForObject|getForEntity|postForObject|postForEntity|"
        r"put|delete|patchForObject|headForHeaders|optionsForAllow|exchange|execute)"
        r"\s*\(",
        block,
    ):
        action = match.group(2)
        args, _ = _call_args(block, match.end() - 1)
        method = _REST_ACTIONS.get(action, "")
        if action in ("exchange", "execute"):
            method = _literal_method(args[1]) if len(args) > 1 else ""
        hits.append(
            _Hit(
                index=match.start(),
                method=method,
                url_expr=args[0] if args else None,
                client="resttemplate",
            )
        )

    # WebClient: .get()/.post()... 与 .uri(expr) 在附近
    for match in re.finditer(r"\.uri\s*\(", block):
        window_start = max(0, match.start() - 500)
        window = block[window_start : match.start()]
        method = ""
        verbs = list(
            re.finditer(
                r"\.(get|post|put|patch|delete|head|options)\s*\(\s*\)", window
            )
        )
        if verbs:
            method = normalize_method(verbs[-1].group(1))
        explicit = list(
            re.finditer(r"\.method\s*\(\s*(?:HttpMethod\.(\w+)|(['\"])(\w+)\2)", window)
        )
        if explicit:
            value = explicit[-1].group(1) or explicit[-1].group(3)
            method = normalize_method(value)
        args, _ = _call_args(block, match.end() - 1)
        if args:
            hits.append(
                _Hit(index=match.start(), method=method, url_expr=args[0], client="webclient")
            )

    # Java 11 HttpClient
    for match in re.finditer(r"HttpRequest\.newBuilder\s*\(\s*\)", block):
        segment = block[match.end() : match.end() + 900]
        uri = re.search(r"\.uri\s*\(", segment)
        if not uri:
            continue
        method = ""
        for pattern in (
            r"\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*\(",
            r"\.method\s*\(\s*(['\"])([A-Za-z]+)\2",
            r"\.method\s*\(\s*HttpMethod\.(\w+)",
        ):
            found = re.search(pattern, segment)
            if found:
                method = normalize_method(found.groups()[-1])
                break
        paren = match.end() + uri.end() - 1
        args, _ = _call_args(block, paren)
        if args:
            hits.append(
                _Hit(index=match.start(), method=method, url_expr=args[0], client="httpclient")
            )

    # OkHttp
    for match in re.finditer(r"new\s+Request\.Builder\s*\(\s*\)", block):
        segment = block[match.end() : match.end() + 700]
        url = re.search(r"\.url\s*\(", segment)
        if not url:
            continue
        method = ""
        for pattern in (
            r"\.(get|post|put|patch|delete|head|options)\s*\(",
            r"\.method\s*\(\s*(['\"])([A-Za-z]+)\2",
        ):
            found = re.search(pattern, segment)
            if found:
                method = normalize_method(found.groups()[-1])
                break
        paren = match.end() + url.end() - 1
        args, _ = _call_args(block, paren)
        if args:
            hits.append(
                _Hit(index=match.start(), method=method, url_expr=args[0], client="okhttp")
            )

    # Apache HttpClient
    for match in re.finditer(
        r"new\s+Http(Get|Post|Put|Delete|Patch|Head|Options)\s*\(", block
    ):
        args, _ = _call_args(block, match.end() - 1)
        hits.append(
            _Hit(
                index=match.start(),
                method=normalize_method(match.group(1)),
                url_expr=args[0] if args else None,
                client="apache-httpclient",
            )
        )
    return hits


_GO_METHODS = {
    "Get": "GET",
    "Post": "POST",
    "PostForm": "POST",
    "Head": "HEAD",
    "Put": "PUT",
    "Patch": "PATCH",
    "Delete": "DELETE",
    "Options": "OPTIONS",
}


def _go_client_vars(file_text: str) -> set[str]:
    names = {"client", "httpClient", "apiClient", "resty", "restClient"}
    for match in re.finditer(
        r"(\w+)\s*(?::=|=)\s*(?:&\s*)?(?:http\.Client|resty\.New|"
        r"http\.Client\{)",
        file_text,
    ):
        names.add(match.group(1))
    for match in re.finditer(
        r"\b(\w+)\s+(?:\*?http\.Client|\*?resty\.Client)\b", file_text
    ):
        names.add(match.group(1))
    return names


def _go_hits(block: str, file_text: str) -> list[_Hit]:
    hits: list[_Hit] = []

    for match in re.finditer(
        r"\bhttp\.(Get|Post|PostForm|Head)\s*\(", block
    ):
        args, _ = _call_args(block, match.end() - 1)
        hits.append(
            _Hit(
                index=match.start(),
                method=_GO_METHODS.get(match.group(1), ""),
                url_expr=args[0] if args else None,
                client="net/http",
            )
        )

    for match in re.finditer(
        r"\bhttp\.NewRequest(?:WithContext)?\s*\(", block
    ):
        args, _ = _call_args(block, match.end() - 1)
        # NewRequest(method, url, body) / NewRequestWithContext(ctx, method, url, body)
        offset = 1 if "WithContext" in match.group(0) else 0
        method = _literal_method(args[offset]) if len(args) > offset else ""
        url_expr = args[offset + 1] if len(args) > offset + 1 else None
        hits.append(
            _Hit(index=match.start(), method=method, url_expr=url_expr, client="net/http")
        )

    # resty: client.R().Get(url)
    for match in re.finditer(
        r"\.R\s*\(\s*\)\s*\.\s*(Get|Post|Put|Patch|Delete|Head|Options)\s*\(",
        block,
    ):
        args, _ = _call_args(block, match.end() - 1)
        hits.append(
            _Hit(
                index=match.start(),
                method=normalize_method(match.group(1)),
                url_expr=args[0] if args else None,
                client="resty",
            )
        )

    clients = _go_client_vars(file_text)
    for match in re.finditer(
        r"\b(\w+)\s*\.\s*(Get|Post|Put|Patch|Delete|Head|Options)\s*\(", block
    ):
        if match.group(1) not in clients:
            continue
        # 避免把 server 路由（r.GET / app.Get）当客户端
        if match.group(2).isupper():
            continue
        args, _ = _call_args(block, match.end() - 1)
        hits.append(
            _Hit(
                index=match.start(),
                method=normalize_method(match.group(2)),
                url_expr=args[0] if args else None,
                client="go-client",
            )
        )
    return hits


def _node_client_vars(file_text: str) -> dict[str, str]:
    bases: dict[str, str] = {}
    for match in re.finditer(
        r"(?:const|let|var)\s+(\w+)\s*=\s*axios\.create\s*\(\s*(\{[\s\S]{0,500}?\})\s*\)",
        file_text,
    ):
        base = _option_string(match.group(2), "baseURL")
        if base:
            bases[match.group(1)] = base
    return bases


def _node_hits(block: str, file_text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    clients = _node_client_vars(file_text)

    for match in re.finditer(r"(?<![\w.])fetch\s*\(", block):
        args, _ = _call_args(block, match.end() - 1)
        if not args:
            continue
        method = _option_string(args[1], "method") if len(args) > 1 else None
        hits.append(
            _Hit(
                index=match.start(),
                method=normalize_method(method or "GET"),
                url_expr=args[0],
                client="fetch",
            )
        )

    for match in re.finditer(
        r"\baxios\s*\.\s*(get|post|put|patch|delete|head|options|request)\s*\(",
        block,
    ):
        args, _ = _call_args(block, match.end() - 1)
        action = match.group(1)
        method = action.upper() if action != "request" else ""
        if action == "request":
            method = _option_string(args[0], "method") if args else None
        hits.append(
            _Hit(
                index=match.start(),
                method=normalize_method(method or ""),
                url_expr=args[0] if args else None,
                client="axios",
            )
        )

    # axios({ url, method }) 与实例方法
    for match in re.finditer(r"\baxios\s*\(\s*\{", block):
        args, _ = _call_args(block, match.end() - 1)
        options = args[0] if args else ""
        url = _option_string(options, "url")
        method = _option_string(options, "method") or "GET"
        if url:
            hits.append(
                _Hit(
                    index=match.start(),
                    method=normalize_method(method),
                    url_expr=f"'{url}'",
                    client="axios",
                )
            )

    for name, base in clients.items():
        for match in re.finditer(
            rf"\b{re.escape(name)}\s*\.\s*(get|post|put|patch|delete|head|options)\s*\(",
            block,
        ):
            args, _ = _call_args(block, match.end() - 1)
            if not args:
                continue
            path = _quoted(args[0])
            merged = f'"{base}" + {args[0]}'
            hits.append(
                _Hit(
                    index=match.start(),
                    method=normalize_method(match.group(1)),
                    url_expr=merged if path else None,
                    client="axios-instance",
                )
            )

    for match in re.finditer(
        r"\bgot\s*(?:\.\s*(get|post|put|patch|delete|head|options))?\s*\(",
        block,
    ):
        args, _ = _call_args(block, match.end() - 1)
        action = match.group(1) or ""
        method = action.upper() if action else ""
        if not action and len(args) > 1:
            method = _option_string(args[1], "method") or "GET"
        hits.append(
            _Hit(
                index=match.start(),
                method=normalize_method(method or "GET"),
                url_expr=args[0] if args else None,
                client="got",
            )
        )

    # Node http/https.request({ hostname, path, method })
    for match in re.finditer(
        r"\bhttps?\s*\.\s*request\s*\(\s*\{", block
    ):
        args, _ = _call_args(block, match.end() - 1)
        options = args[0] if args else ""
        host = _option_string(options, "hostname") or _option_string(options, "host")
        path = _option_string(options, "path")
        method = _option_string(options, "method") or "GET"
        if not host and not path:
            continue
        host_expr = f"'{host}'" if host else None
        path_expr = f"'{path}'" if path else None
        if host_expr and path_expr:
            url_expr = f"{host_expr} + {path_expr}"
        elif path_expr:
            url_expr = path_expr
        else:
            url_expr = host_expr
        hits.append(
            _Hit(
                index=match.start(),
                method=normalize_method(method),
                url_expr=url_expr,
                client="node-http",
            )
        )
    return hits


def _dedupe_hits(hits: list[_Hit]) -> list[_Hit]:
    seen: set[tuple[int, str, str]] = set()
    result: list[_Hit] = []
    for hit in hits:
        key = (hit.index, hit.method, hit.url_expr or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(hit)
    return sorted(result, key=lambda item: item.index)


# ───────────────────────── 对外入口 ─────────────────────────


def scan_language_file(
    *,
    path: Path,
    rel: str,
    language: str,
    handler: str,
    line_hint: int = 0,
    file_text: str | None = None,
    max_sites: int = 200,
) -> LangScan:
    """扫描一个 handler 对应的 Java/Go/Node 源码块。只读文本，不执行。"""
    result = LangScan()
    try:
        raw = file_text if file_text is not None else path.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        result.warnings.append(f"source_read_error:{path.name}:{type(exc).__name__}")
        return result
    text = _mask_comments(raw)
    if not text.strip():
        return result

    block_info = _named_block(text, handler, language, line_hint)
    if block_info is None:
        block_info = _enclosing_block(text, line_hint)
    if block_info is None:
        result.warnings.append(f"handler_source_missing:{handler or path.name}")
        return result
    start, end, block_line = block_info
    block = text[start:end]
    if len(block) > MAX_BLOCK_CHARS:
        block = block[:MAX_BLOCK_CHARS]
        end = start + MAX_BLOCK_CHARS
        result.warnings.append(f"source_block_truncated:{rel}")
    if not block.strip():
        return result

    file_vars = _collect_bindings(text, language)
    local_vars = _collect_bindings(block, language)
    variables: dict[str, list[str]] = {
        name: list(values) for name, values in file_vars.items()
    }
    for name, values in local_vars.items():
        variables.setdefault(name, []).extend(values)
    evaluator = _Evaluator(language, variables)

    if language == "java":
        hits = _java_hits(block)
        result.local_callees = _block_callees(
            block, language, _function_names(text, language)
        )
    elif language == "go":
        hits = _go_hits(block, text)
        result.local_callees = _block_callees(
            block, language, _function_names(text, language)
        )
    else:
        hits = _node_hits(block, text)
        result.local_callees = _block_callees(
            block, language, _function_names(text, language)
        )

    for hit in _dedupe_hits(hits)[:max_sites]:
        position = start + hit.index
        line = _line_of(text, position)
        column = position - _index_for_line(text, line)
        call_site_id = f"{rel}:{line}:{column}"
        evidence = evaluator.evaluate(hit.url_expr) if hit.url_expr else _dynamic("url_missing")
        target = evidence.target if evidence.kind == "target" else None
        path_value = (
            evidence.path if evidence.kind in ("path", "config", "dynamic") else None
        )
        if target is None and path_value is None:
            result.unsupported_reasons[call_site_id] = (
                evidence.detail or "target_unresolved"
            )
        result.sites.append(
            HttpCallSite(
                call_site_id=call_site_id,
                handler=str(handler or ""),
                file=rel,
                line=line,
                method=hit.method,
                target=target,
                path=path_value,
                config_key=evidence.config_key,
                client=hit.client,
            )
        )
    return result


__all__ = [
    "LANGUAGE_BY_SUFFIX",
    "LangScan",
    "language_for",
    "scan_language_file",
]
