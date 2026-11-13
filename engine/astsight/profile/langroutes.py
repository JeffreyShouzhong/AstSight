"""Java / Go / Node 路由声明的轻量静态提取。

`codegraph.route_repair` 只覆盖 Python 框架；非 Python 项目在没有图谱或图谱
返回空时，接口清单会整体缺失，下游 Mock 也就无从挂载。这里用保守的文本
扫描补上常见写法：

- Java/Kotlin：Spring `@RestController`/`@Controller` + 类级 `@RequestMapping`
  + 方法级 `@GetMapping`/`@PostMapping`/`@RequestMapping(method=...)`；
- Go：Gin、Echo、Chi、Fiber、Gorilla mux、`net/http` 的注册调用与 `Group` 前缀；
- Node：Express、Fastify、Koa Router、Hono 的 `app.get('/x', handler)`，
  以及 NestJS 的 `@Controller` + `@Get` 装饰器。

只接受字面量路径；路径里出现变量或模板时跳过并告警，不猜。产出结构与
`RoutePrefixRepair` 的 `as_dict()` 对齐，供 `inventory_routes` 合并。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .langscan import LANGUAGE_BY_SUFFIX

MAX_FILES = 2_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ROUTES = 2_000

_SKIP_DIRS = {
    ".git",
    ".astsight",
    ".codegraph",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "target",
    "vendor",
    "coverage",
}

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
_SPRING_MAPPING = {
    "GetMapping": ["GET"],
    "PostMapping": ["POST"],
    "PutMapping": ["PUT"],
    "PatchMapping": ["PATCH"],
    "DeleteMapping": ["DELETE"],
}
_SPRING_METHOD = re.compile(r"RequestMethod\.([A-Z]+)")
_NEST_MAPPING = {
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Patch": "PATCH",
    "Delete": "DELETE",
    "Head": "HEAD",
    "Options": "OPTIONS",
}


@dataclass(frozen=True)
class LanguageRoute:
    method: str
    path: str
    handler: str
    handler_owner: str
    file: str
    line: int
    framework: str
    confidence: str = "verified"
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "handler": self.handler,
            "handler_owner": self.handler_owner,
            "file": self.file,
            "route_file": self.file,
            "line": self.line,
            "route_line": self.line,
            "source": "framework_plugin",
            "confidence": self.confidence,
            "framework": self.framework,
            "note": self.note,
        }


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, max(0, index)) + 1


def _mask_comments(text: str) -> str:
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


def _normalize_path(*parts: str) -> str:
    segments: list[str] = []
    for part in parts:
        for segment in str(part or "").split("/"):
            segment = segment.strip()
            if segment:
                segments.append(segment)
    path = "/" + "/".join(segments) if segments else "/"
    path = re.sub(r":([A-Za-z_]\w*)", r"{\1}", path)
    path = re.sub(r"\*([A-Za-z_]\w*)", r"{\1}", path)
    return path


def _quoted_literal(text: str) -> str | None:
    match = re.fullmatch(r"\s*(['\"`])([^'\"`]*)\1\s*", str(text or ""))
    if not match:
        return None
    inner = match.group(2)
    if "${" in inner or "{{" in inner:
        return None
    return inner


def _first_quoted(annotation: str) -> str | None:
    match = re.search(r"(['\"])([^'\"]*)\1", annotation)
    if not match:
        return None
    inner = match.group(2)
    if "${" in inner:
        return None
    return inner


def _handler_name(expr: str) -> str:
    value = str(expr or "").strip()
    if not value:
        return ""
    value = value.split("(", 1)[0].strip()
    value = value.lstrip("&")
    value = value.rsplit(".", 1)[-1]
    value = re.sub(r"[^\w$]", "", value)
    if value in {"func", "function", "new", "make", "return"}:
        return ""
    return value


class _Walker:
    def __init__(self, root: Path):
        self.root = root
        self.warnings: list[str] = []
        self.files: list[tuple[Path, str, str]] = []

    def collect(self) -> list[tuple[Path, str, str]]:
        result: list[tuple[Path, str, str]] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SKIP_DIRS and not name.startswith(".")
            )
            for filename in sorted(filenames):
                language = LANGUAGE_BY_SUFFIX.get(Path(filename).suffix.lower())
                if not language:
                    continue
                path = Path(dirpath) / filename
                try:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(self.root):
                        continue
                    size = resolved.stat().st_size
                except OSError:
                    continue
                if size > MAX_FILE_BYTES:
                    self.warnings.append(
                        f"route_source_skipped_large:{path.relative_to(self.root)}"
                    )
                    continue
                if total + size > MAX_TOTAL_BYTES:
                    self.warnings.append("route_source_scan_truncated_total_bytes")
                    return result
                total += size
                result.append(
                    (resolved, language, resolved.relative_to(self.root).as_posix())
                )
                if len(result) >= MAX_FILES:
                    self.warnings.append("route_source_scan_truncated_file_count")
                    return result
        return result


class LanguageRouteScanner:
    def __init__(self, project_root: str | Path):
        self.root = Path(project_root).resolve()
        self.warnings: list[str] = []

    def scan(self) -> list[LanguageRoute]:
        walker = _Walker(self.root)
        files = walker.collect()
        self.warnings.extend(walker.warnings)
        routes: list[LanguageRoute] = []
        seen: set[tuple[str, str, str]] = set()
        for path, language, rel in files:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self.warnings.append(
                    f"route_source_read_error:{rel}:{type(exc).__name__}"
                )
                continue
            text = _mask_comments(raw)
            if language == "java":
                found = _java_routes(text, rel)
            elif language == "go":
                found = _go_routes(text, rel)
            else:
                found = _node_routes(text, rel)
            for route in found:
                key = (route.method, route.path, route.handler)
                if key in seen:
                    continue
                seen.add(key)
                routes.append(route)
                if len(routes) >= MAX_ROUTES:
                    self.warnings.append("route_source_truncated_route_count")
                    return routes
        return routes


# ───────────────────────── Java / Kotlin ─────────────────────────


_JAVA_CLASS = re.compile(r"\b(?:class|object)\s+(\w+)")
_JAVA_ANNOTATION = re.compile(r"@(\w+)(?:\s*\((.*?)\))?", re.S)
_JAVA_METHOD = re.compile(
    r"(?m)(?:^|[;{}])\s*"
    r"(?:(?:public|private|protected|static|final|synchronized|abstract|"
    r"default|native|override|suspend|inline)\s+)*"
    r"[\w<>\[\],.?@\s]+?\s+(\w+)\s*\([^;{}]*\)\s*(?:throws[^{]+)?\{"
)
_KOTLIN_FUN = re.compile(
    r"(?m)^\s*(?:(?:public|private|protected|internal|override|suspend|inline|"
    r"open|final)\s+)*fun\s+(\w+)\s*\("
)


def _spring_class_prefix(annotations: str) -> str:
    match = re.search(
        r"@RequestMapping\s*\(\s*(?:value\s*=\s*)?(['\"])([^'\"]*)\1",
        annotations,
    )
    if match and "${" not in match.group(2):
        return match.group(2)
    return ""


def _spring_methods(annotation: str) -> list[str]:
    name_match = re.match(r"@(\w+)", annotation)
    if not name_match:
        return []
    name = name_match.group(1)
    if name in _SPRING_MAPPING:
        return list(_SPRING_MAPPING[name])
    methods = _SPRING_METHOD.findall(annotation)
    if methods:
        return [method for method in methods if method in _HTTP_METHODS]
    return []


def _java_routes(text: str, rel: str) -> list[LanguageRoute]:
    routes: list[LanguageRoute] = []
    for class_match in _JAVA_CLASS.finditer(text):
        owner = class_match.group(1)
        brace = text.find("{", class_match.end())
        if brace < 0:
            continue
        # 类注解区：从上一个 } 或文件头到 class 关键字
        head_start = max(text.rfind("}", 0, class_match.start()), 0)
        head = text[head_start : class_match.start()]
        if not re.search(r"@(?:Rest)?Controller\b", head):
            continue
        prefix = _spring_class_prefix(head)
        end = _matching_brace(text, brace)
        body_start = brace + 1
        body = text[body_start:end]

        markers: list[tuple[int, int, str, str]] = []
        for match in _JAVA_ANNOTATION.finditer(body):
            annotation = match.group(0)
            methods = _spring_methods(annotation)
            if not methods:
                continue
            path = _first_quoted(annotation)
            if path is None and match.group(2) and "value" in match.group(2):
                path = _first_quoted(match.group(2))
            markers.append((match.start(), match.end(), "|".join(methods), path or ""))

        for index, (_, annot_end, methods, path) in enumerate(markers):
            method_start = annot_end
            method_end = (
                markers[index + 1][0] if index + 1 < len(markers) else len(body)
            )
            region = body[method_start:method_end]
            handler_match = _JAVA_METHOD.search(region) or _KOTLIN_FUN.search(region)
            if handler_match is None:
                continue
            handler = handler_match.group(1)
            line = _line_of(text, body_start + method_start + handler_match.start())
            for method in methods.split("|"):
                if method not in _HTTP_METHODS:
                    continue
                routes.append(
                    LanguageRoute(
                        method=method,
                        path=_normalize_path(prefix, path),
                        handler=handler,
                        handler_owner=owner,
                        file=rel,
                        line=line,
                        framework="spring",
                        confidence="verified" if path or prefix else "inferred",
                        note="" if path else "class_prefix_only",
                    )
                )
    return routes


# ───────────────────────── Go ─────────────────────────


_GO_VERBS = "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Any|Get|Post|Put|Patch|Delete|Head|Options|Any"
_GO_ROUTER_CTOR = re.compile(
    r"(\w+)\s*:?=\s*(?:gin\.(?:New|Default)|echo\.New|fiber\.New|chi\.NewRouter|"
    r"http\.NewServeMux|mux\.NewRouter|mux\.NewRoute|httprouter\.New)\s*\("
)
_GO_ROUTER_DECL = re.compile(
    r"\b(\w+)\s+(?:\*?(?:gin|echo|fiber|chi|mux)\.\w+)\b"
)
_GO_GROUP = re.compile(
    r"(\w+)\s*:?=\s*(\w+)\s*\.\s*Group\s*\(\s*(['\"`])([^'\"`]*)\3"
)
_GO_REGISTER = re.compile(
    rf"\b(\w+)\s*\.\s*({_GO_VERBS}|HandleFunc|Handle)\s*\(\s*(['\"`])([^'\"`]*)\3\s*,\s*([^)\n]+)"
)
_GO_METHODS_CHAIN = re.compile(r"\.\s*Methods\s*\(([^)]*)\)")


def _go_prefixes(text: str) -> dict[str, str]:
    groups: dict[str, tuple[str, str]] = {}
    for match in _GO_GROUP.finditer(text):
        groups[match.group(1)] = (match.group(2), match.group(4))

    def resolve(name: str, depth: int = 0) -> str:
        if depth > 8 or name not in groups:
            return ""
        parent, prefix = groups[name]
        parent_prefix = resolve(parent, depth + 1)
        return _normalize_path(parent_prefix, prefix).rstrip("/")

    return {name: resolve(name) for name in groups}


def _go_routers(text: str) -> set[str]:
    names = set(_GO_ROUTER_CTOR.findall(text))
    names.update(_GO_ROUTER_DECL.findall(text))
    if re.search(r"\b(?:gin|echo|fiber|chi|mux|httprouter)\b", text):
        names.update({"r", "router", "app", "e", "g", "mux", "api", "engine", "srv"})
    return names


def _go_routes(text: str, rel: str) -> list[LanguageRoute]:
    routers = _go_routers(text)
    if not routers:
        return []
    frameworks = []
    if "gin." in text:
        frameworks.append("gin")
    if "echo." in text:
        frameworks.append("echo")
    if "fiber." in text:
        frameworks.append("fiber")
    if "chi." in text:
        frameworks.append("chi")
    if "mux." in text or "httprouter." in text:
        frameworks.append("mux")
    if "http.NewServeMux" in text or "http.HandleFunc" in text:
        frameworks.append("net/http")
    framework = ",".join(frameworks) or "go-router"
    prefixes = _go_prefixes(text)
    routes: list[LanguageRoute] = []

    for match in _GO_REGISTER.finditer(text):
        var, verb, path = match.group(1), match.group(2), match.group(4)
        if var not in routers:
            continue
        tail = match.group(5) or ""
        handler = _handler_name(tail.split(",")[0])
        if not handler or handler in ("nil", "http.HandlerFunc"):
            continue
        methods: list[str] = []
        if verb.lower() == "any":
            methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
        elif verb.lower() in ("handlefunc", "handle"):
            # net/http ServeMux 支持 "GET /path" 形式
            method_match = re.match(
                rf"^({_GO_VERBS})\s+(/.*)$", path, re.I
            )
            if method_match:
                method = method_match.group(1).upper()
                path = method_match.group(2)
                methods = [method] if method in _HTTP_METHODS else []
            else:
                methods = ["GET"]
        else:
            method = verb.upper()
            methods = [method] if method in _HTTP_METHODS else []

        # Gorilla: .HandleFunc(...).Methods("GET", "POST")
        window = text[match.end() : match.end() + 200]
        chain = _GO_METHODS_CHAIN.search(window)
        if chain:
            methods = [
                token.strip().strip('"\'').upper()
                for token in chain.group(1).split(",")
                if token.strip().strip('"\'').upper() in _HTTP_METHODS
            ]
        if not methods:
            continue
        path_line = _line_of(text, match.start())
        for method in methods:
            routes.append(
                LanguageRoute(
                    method=method,
                    path=_normalize_path(prefixes.get(var, ""), path),
                    handler=handler,
                    handler_owner="",
                    file=rel,
                    line=path_line,
                    framework=framework,
                    confidence="verified",
                )
            )
    return routes


# ───────────────────────── Node ─────────────────────────


_NODE_ROUTER_CTOR = re.compile(
    r"(\w+)\s*:?=\s*(?:"
    r"(?:express|require\(['\"]express['\"]\))\s*\(\s*\)|"
    r"(?:express|require\(['\"]express['\"]\))\s*\.\s*Router\s*\(\s*\)|"
    r"(?:fastify|Fastify)\s*\(|"
    r"new\s+(?:Router|KoaRouter)\s*\(\s*\)|"
    r"new\s+Hono\s*\("
    r")"
)
_NODE_USE = re.compile(
    r"\b(\w+)\s*\.\s*use\s*\(\s*(['\"`])([^'\"`]*)\2\s*,\s*(\w+)\s*\)"
)
_NODE_VERB_CALL = re.compile(
    r"\b(\w+)\s*\.\s*(get|post|put|patch|delete|head|options|all)\s*\(\s*"
    r"(['\"`])([^'\"`]*)\3\s*,\s*",
    re.I,
)
_NODE_ROUTE_OBJECT = re.compile(r"\b(\w+)\s*\.\s*route\s*\(\s*\{", re.S)
_NODE_FETCHLIKE = {"axios", "fetch", "got", "http", "https", "request", "superagent"}


def _node_routers(text: str) -> set[str]:
    names = set(_NODE_ROUTER_CTOR.findall(text))
    if re.search(r"require\(['\"]express['\"]\)|from\s+['\"]express['\"]", text):
        names.update({"app", "router", "api"})
    if re.search(r"from\s+['\"]fastify['\"]|require\(['\"]fastify['\"]\)", text):
        names.update({"fastify", "app", "server"})
    if re.search(r"require\(['\"]koa-router['\"]\)|@koa/router|from\s+['\"]koa-router['\"]", text):
        names.update({"router", "api"})
    if re.search(r"from\s+['\"]hono['\"]|require\(['\"]hono['\"]\)", text):
        names.update({"app", "router"})
    return names


def _node_use_prefixes(text: str) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for match in _NODE_USE.finditer(text):
            parent, _quote, prefix, child = match.groups()
            base = prefixes.get(parent, "")
            value = _normalize_path(base, prefix).rstrip("/")
            if prefixes.get(child) != value:
                prefixes[child] = value
                changed = True
    return prefixes


def _top_level_args(text: str) -> list[tuple[str, int]]:
    """把 `a, (b, c) => d, e)` 前的参数按顶层逗号切开，返回 (参数, 偏移)。"""
    args: list[tuple[str, int]] = []
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
            if depth == 0:
                arg = text[start:index].strip()
                if arg:
                    args.append((arg, start))
                return args
            depth -= 1
        elif ch == "," and depth == 0:
            arg = text[start:index].strip()
            if arg:
                args.append((arg, start))
            start = index + 1
    arg = text[start:].strip()
    if arg:
        args.append((arg, start))
    return args


def _node_handler(after: str) -> tuple[str, int | None]:
    """返回 (handler 名, 回调首条语句的行偏移)。

    行偏移为 None 表示无法安全定位回调体（单行箭头、无花括号），此时不扫描
    该回调，避免把注册所在函数的调用误挂到接口上。
    """
    args = _top_level_args(after)
    if not args:
        return "", None
    last, offset = args[-1]
    # 去掉 leading 空白后 offset 会偏移，重新定位原始片段
    raw_start = after.find(last, offset)
    if raw_start < 0:
        raw_start = offset
    stripped = re.sub(r"^async\s+", "", last).strip()
    if (
        stripped.startswith("function")
        or stripped.startswith("(")
        or re.match(r"^[\w$.]+\s*=>", stripped)
    ):
        arrow_at = last.find("=>")
        brace = last.find("{", arrow_at) if arrow_at >= 0 else last.find("{")
        if brace >= 0:
            head = after[: raw_start + brace + 1]
            tail = after[raw_start + brace + 1 :]
            first = re.search(r"\S", tail)
            if first is None:
                return "", None
            if tail[: first.start()].count("\n") == 0:
                return "", None  # 单行回调：没有可靠的行定位
            return "", head.count("\n") + tail[: first.start()].count("\n")
        arrow = stripped.split("=>", 1)[-1].strip()
        return _handler_name(arrow), 0
    return _handler_name(last), 0


def _node_routes(text: str, rel: str) -> list[LanguageRoute]:
    routers = _node_routers(text)
    if not routers:
        return []
    prefixes = _node_use_prefixes(text)
    if "fastify" in text.lower():
        framework = "fastify"
    elif "hono" in text:
        framework = "hono"
    elif "koa" in text.lower():
        framework = "koa"
    else:
        framework = "express"

    routes: list[LanguageRoute] = []

    # NestJS 装饰器
    routes.extend(_nest_routes(text, rel))

    for match in _NODE_VERB_CALL.finditer(text):
        var, verb, _quote, path = match.groups()
        if var not in routers or var in _NODE_FETCHLIKE:
            continue
        after = text[match.end() : match.end() + 600]
        name, extra_lines = _node_handler(after)
        line = (
            _line_of(text, match.start()) + extra_lines
            if extra_lines is not None
            else 0
        )
        methods = (
            ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
            if verb.lower() == "all"
            else [verb.upper()]
        )
        for method in methods:
            routes.append(
                LanguageRoute(
                    method=method,
                    path=_normalize_path(prefixes.get(var, ""), path),
                    handler=name,
                    handler_owner="" if name else var,
                    file=rel,
                    line=line,
                    framework=framework,
                    confidence="verified" if name or line else "inferred",
                    note=(
                        ""
                        if name
                        else ("inline_handler" if line else "inline_handler_unscanned")
                    ),
                )
            )

    for match in _NODE_ROUTE_OBJECT.finditer(text):
        var = match.group(1)
        if var not in routers:
            continue
        brace = text.find("{", match.end() - 1)
        if brace < 0:
            continue
        end = _matching_brace(text, brace)
        body = text[brace + 1 : end]
        method_match = re.search(
            r"\bmethod\s*:\s*(['\"])([A-Za-z]+)\1", body
        )
        url_match = re.search(r"\burl\s*:\s*(['\"`])([^'\"`]*)\1", body)
        handler_match = re.search(r"\bhandler\s*:\s*([\w$.]+)", body)
        if not method_match or not url_match:
            continue
        method = method_match.group(2).upper()
        if method not in _HTTP_METHODS:
            continue
        handler = _handler_name(handler_match.group(1)) if handler_match else ""
        routes.append(
            LanguageRoute(
                method=method,
                path=_normalize_path(prefixes.get(var, ""), url_match.group(2)),
                handler=handler,
                handler_owner="" if handler else var,
                file=rel,
                line=_line_of(text, match.start()),
                framework=framework,
                confidence="verified" if handler else "inferred",
                note="" if handler else "inline_handler",
            )
        )
    return routes


_NEST_CONTROLLER = re.compile(
    r"@Controller\s*\(\s*(?:(['\"])([^'\"]*)\1)?\s*\)"
)
_NEST_METHOD = re.compile(r"@(Get|Post|Put|Patch|Delete|Head|Options)\s*\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)")


def _nest_routes(text: str, rel: str) -> list[LanguageRoute]:
    controller = _NEST_CONTROLLER.search(text)
    if not controller:
        return []
    prefix = controller.group(2) or ""
    routes: list[LanguageRoute] = []
    markers = list(_NEST_METHOD.finditer(text, controller.end()))
    for index, match in enumerate(markers):
        region_end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        region = text[match.end() : region_end]
        handler_match = re.search(
            r"\b(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::[^{]+)?\{", region
        )
        if handler_match is None:
            continue
        method = _NEST_MAPPING.get(match.group(1), "")
        if not method:
            continue
        path = match.group(2) or ""
        routes.append(
            LanguageRoute(
                method=method,
                path=_normalize_path(prefix, path),
                handler=handler_match.group(1),
                handler_owner="",
                file=rel,
                line=_line_of(text, match.end() + handler_match.start()),
                framework="nestjs",
                confidence="verified",
                note="" if path else "class_prefix_only",
            )
        )
    return routes


def scan_language_routes(
    project_root: str | Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (路由字典列表, 告警列表)。只读源码，不执行。"""
    scanner = LanguageRouteScanner(project_root)
    routes = scanner.scan()
    return [route.as_dict() for route in routes], scanner.warnings


__all__ = ["LanguageRoute", "LanguageRouteScanner", "scan_language_routes"]
