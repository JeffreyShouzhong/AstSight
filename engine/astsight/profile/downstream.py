"""C3 下游 HTTP 调用点识别（P1，仅明文 HTTP）。

从路由 handler 出发，在项目源码上做有界静态扫描：

- **Python**：AST 扫描 `requests` / `httpx` / `urllib`，解析字面量、环境变量/
  配置键拼接、`urljoin`、f-string 与 `str.format` 模板；
- **Java/Kotlin、Go、Node/TS**：`langscan` 的轻量文本扫描（RestTemplate、
  WebClient、OkHttp、Apache HttpClient、net/http、resty、fetch、axios、got
  等），覆盖字面量、拼接、配置键与模板字符串；
- 无法确定地址或 method 的调用点进入 `unsupported`，**不猜**。

本模块只读源码，不 import、不执行被测项目。扫描有文件数/大小/符号数上限，
路径一律经 `resolve()` 后校验仍在项目根内；越界或超限只告警，不中断画像。

`dep_id` 由 `astsight.mock.identity.build_dep_id_plan` 统一计算，本模块只负责
产出 `CallSite` 证据。
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ..mock.identity import (
    CallSite,
    build_dep_id_plan,
    normalize_method,
)
from .callsite import HttpCallSite
from .langscan import (
    _line_of,
    _matching_brace,
    _NOT_CALLEES,
    language_for,
    scan_language_file,
)


MAX_FILES = 2_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SYMBOLS = 40
MAX_DEPTH = 3
MAX_CALL_SITES = 200
MAX_FUNCTIONS_PER_FILE = 500
MAX_VARIABLE_ASSIGNMENTS = 8

_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
_CLIENT_FACTORIES = {
    ("requests", "Session"),
    ("requests", "session"),
    ("httpx", "Client"),
    ("httpx", "AsyncClient"),
}
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
}
_SETTINGS_ROOTS = {"settings", "config", "cfg", "conf", "options", "app_config"}
_FORMAT_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_SUPPORTED_LANGUAGES = ("python", "java", "go", "node")
_LANGUAGE_ALIASES = {
    "python": "python",
    "py": "python",
    "java": "java",
    "kotlin": "java",
    "kt": "java",
    "go": "go",
    "golang": "go",
    "node": "node",
    "nodejs": "node",
    "javascript": "node",
    "typescript": "node",
    "js": "node",
    "ts": "node",
    "jsx": "node",
    "tsx": "node",
}
_NAME_SPLIT = re.compile(r"::|#|/|\$|\\")


@dataclass
class DownstreamScan:
    """一个 handler 的下游扫描结果。"""

    handler: str
    call_sites: list[HttpCallSite] = field(default_factory=list)
    deps: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    checked: bool = False


@dataclass
class _Function:
    module: "_Module"
    node: ast.FunctionDef | ast.AsyncFunctionDef
    name: str
    qualname: str
    class_name: str = ""


@dataclass
class _Module:
    path: Path
    rel: str
    tree: ast.Module
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    vars: dict[str, list[ast.expr]] = field(default_factory=dict)
    clients: dict[str, str] = field(default_factory=dict)
    functions: list[_Function] = field(default_factory=list)


@dataclass
class _Scope:
    module: _Module
    vars: dict[str, list[ast.expr]] = field(default_factory=dict)
    clients: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _LangSymbol:
    """Java/Go/Node 源文件中声明的函数/方法。"""

    path: Path
    rel: str
    language: str
    name: str
    owner: str
    line: int


@dataclass
class _UrlEval:
    kind: str  # target / path / config / dynamic
    target: str | None = None
    path: str | None = None
    config_key: str | None = None
    detail: str = ""

    def equivalent(self, other: "_UrlEval") -> bool:
        return (
            self.kind == other.kind
            and self.target == other.target
            and self.path == other.path
            and self.config_key == other.config_key
        )


def _dynamic(detail: str = "", *, path: str | None = None) -> _UrlEval:
    return _UrlEval("dynamic", path=path, detail=detail)


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


# ───────────────────────── 表达式分类 ─────────────────────────


def _literal(text: str) -> _UrlEval:
    value = text.strip()
    if not value:
        return _dynamic("empty_url")
    if "://" in value:
        scheme = value.split("://", 1)[0].lower()
        if scheme in ("http", "https"):
            authority = value.split("://", 1)[1].split("/", 1)[0]
            if not authority or "{" in authority:
                return _dynamic("dynamic_host")
            return _UrlEval("target", target=value)
        return _dynamic("unsupported_scheme")
    if value.startswith("/"):
        return _UrlEval("path", path=value)
    return _dynamic("relative_url")


def _join_target(base: str, suffix: str) -> str:
    left = base.rstrip("/")
    right = suffix if suffix.startswith("/") else "/" + suffix
    return left + right


def _combine(left: _UrlEval, right: _UrlEval) -> _UrlEval:
    if left.kind == "target":
        if right.kind == "path" and right.path:
            return _UrlEval("target", target=_join_target(left.target or "", right.path))
        if right.kind == "dynamic" and right.path:
            return _dynamic(right.detail or "dynamic_suffix")
        return _dynamic("dynamic_suffix")
    if left.kind == "path":
        if right.kind == "path" and left.path and right.path:
            return _UrlEval("path", path=left.path + right.path)
        return _dynamic("dynamic_suffix", path=left.path)
    if left.kind == "config":
        if right.kind == "path" and right.path:
            return _UrlEval(
                "config", path=right.path, config_key=left.config_key
            )
        if right.kind == "dynamic" and right.path:
            return _UrlEval(
                "config",
                path=right.path,
                config_key=left.config_key,
                detail=right.detail,
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


class _ExpressionEvaluator:
    """对 URL 表达式做保守求值；无法证明时返回 dynamic。"""

    def __init__(self, scope: _Scope, handler: str):
        self.scope = scope
        self.handler = handler

    def evaluate(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> _UrlEval:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return _literal(node.value)
            return _dynamic("not_a_string")

        if isinstance(node, ast.Name):
            return self._evaluate_name(node.id, seen)

        if isinstance(node, ast.JoinedStr):
            return self._evaluate_joined(node, seen)

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._evaluate_concat(node, seen)

        if isinstance(node, ast.Subscript):
            key = self._env_key(node)
            if key:
                return _UrlEval("config", config_key=key)
            settings = self._settings_key(node)
            if settings:
                return _UrlEval("config", config_key=settings)
            return _dynamic("dynamic_subscript")

        if isinstance(node, ast.Attribute):
            key = self._env_key(node) or self._settings_key(node)
            if key:
                return _UrlEval("config", config_key=key)
            return _dynamic("dynamic_attribute")

        if isinstance(node, ast.Call):
            return self._evaluate_call(node, seen)

        if isinstance(node, ast.Await):
            return self.evaluate(node.value, seen)

        if isinstance(node, ast.IfExp):
            left = self.evaluate(node.body, seen)
            right = self.evaluate(node.orelse, seen)
            if left.equivalent(right):
                return left
            if left.path and left.path == right.path and left.kind == right.kind:
                return left
            return _dynamic("conditional_url")

        if isinstance(node, ast.Starred):
            return self.evaluate(node.value, seen)

        return _dynamic("unsupported_expression")

    # ---- 变量与拼接 ----

    def _evaluate_name(self, name: str, seen: frozenset[str]) -> _UrlEval:
        if name in seen:
            return _dynamic("cyclic_variable")
        assignments = self.scope.vars.get(name) or []
        if not assignments:
            return _dynamic("target_dynamic")
        results = [
            self.evaluate(expr, seen | {name})
            for expr in assignments[-MAX_VARIABLE_ASSIGNMENTS:]
        ]
        if not results:
            return _dynamic("target_dynamic")
        first = results[0]
        if all(first.equivalent(item) for item in results[1:]):
            return first
        return _dynamic("variable_reassigned")

    def _evaluate_joined(
        self, node: ast.JoinedStr, seen: frozenset[str]
    ) -> _UrlEval:
        chunks: list[str] = []
        config_keys: list[str] = []
        first_is_config = False
        for index, value in enumerate(node.values):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                chunks.append(value.value)
                continue
            if not isinstance(value, ast.FormattedValue):
                chunks.append("{}")
                continue
            inner = self.evaluate(value.value, seen)
            if inner.kind == "config" and inner.config_key:
                config_keys.append(inner.config_key)
                chunks.append("{}")
                if index == 0:
                    first_is_config = True
            elif inner.kind == "target" and inner.target:
                chunks.append(inner.target)
            elif inner.kind == "path" and inner.path:
                chunks.append(inner.path)
            else:
                chunks.append("{}")
        skeleton = "".join(chunks)
        result = _literal(skeleton)
        if result.kind == "target":
            return result
        if result.kind == "path":
            return result

        unique_keys = _dedupe(config_keys)
        if len(unique_keys) > 1:
            return _dynamic("config_key_conflict")
        if unique_keys and first_is_config:
            remainder = skeleton[2:]
            if remainder.startswith("/"):
                return _UrlEval(
                    "config", path=remainder, config_key=unique_keys[0]
                )
            if not remainder:
                return _UrlEval("config", config_key=unique_keys[0])
        if unique_keys and "://" not in skeleton:
            return _UrlEval(
                "config",
                config_key=unique_keys[0],
                path=skeleton if skeleton.startswith("/") else None,
                detail="dynamic_prefix",
            )
        if "://" in skeleton and "{" in skeleton.split("://", 1)[1].split("/", 1)[0]:
            tail = skeleton.split("://", 1)[1]
            if "/" in tail:
                path = "/" + tail.split("/", 1)[1]
                if "{" not in path:
                    return _dynamic("dynamic_host", path=path)
        return result if result.kind == "dynamic" else _dynamic("target_dynamic")

    def _evaluate_concat(self, node: ast.BinOp, seen: frozenset[str]) -> _UrlEval:
        operands: list[ast.expr] = []

        def flatten(item: ast.expr) -> None:
            if isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
                flatten(item.left)
                flatten(item.right)
            else:
                operands.append(item)

        flatten(node)
        result: _UrlEval | None = None
        for operand in operands:
            current = self.evaluate(operand, seen)
            result = current if result is None else _combine(result, current)
            if result.kind == "dynamic" and not result.path:
                return result
        return result or _dynamic("target_dynamic")

    # ---- 调用表达式 ----

    def _evaluate_call(self, node: ast.Call, seen: frozenset[str]) -> _UrlEval:
        env_key = self._env_key(node)
        if env_key:
            return _UrlEval("config", config_key=env_key)
        settings = self._settings_key(node)
        if settings:
            return _UrlEval("config", config_key=settings)

        func = node.func
        if isinstance(func, ast.Name) and func.id in ("str", "repr"):
            if node.args:
                return self.evaluate(node.args[0], seen)
            return _dynamic("target_dynamic")
        if isinstance(func, ast.Attribute):
            if func.attr in ("format", "join") and isinstance(func.value, ast.Constant):
                template = func.value.value
                if isinstance(template, str):
                    if func.attr == "format":
                        return _literal(_FORMAT_PLACEHOLDER.sub("{}", template))
                    return _literal(template)
            if func.attr == "join" and isinstance(func.value, ast.Constant):
                return _dynamic("target_dynamic")
            if func.attr == "urljoin" and len(node.args) >= 2:
                return self._evaluate_urljoin(node, seen)
        if isinstance(func, ast.Name) and func.id == "urljoin" and len(node.args) >= 2:
            return self._evaluate_urljoin(node, seen)
        if isinstance(func, ast.Attribute) and func.attr in ("rstrip", "strip", "lstrip"):
            if func.value is not None:
                return self.evaluate(func.value, seen)
        return _dynamic("dynamic_call")

    def _evaluate_urljoin(self, node: ast.Call, seen: frozenset[str]) -> _UrlEval:
        base = self.evaluate(node.args[0], seen)
        suffix = self.evaluate(node.args[1], seen)
        if base.kind == "target" and suffix.kind == "path" and suffix.path:
            return _UrlEval(
                "target", target=urljoin(base.target or "", suffix.path)
            )
        if base.kind == "target" and suffix.kind == "target" and suffix.target:
            return _UrlEval(
                "target", target=urljoin(base.target or "", suffix.target)
            )
        return _combine(base, suffix)

    # ---- 配置键识别 ----

    def _env_key(self, node: ast.expr) -> str | None:
        module = self.scope.module
        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "environ"
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
            ):
                return self._constant_key(node.slice)
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                root = self._dotted_name(value)
                if root and root.split(".", 1)[0] in _SETTINGS_ROOTS:
                    return f"{root}.{node.slice.value}"
            return None

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                owner = func.value
                if func.attr == "getenv" and isinstance(owner, ast.Name) and owner.id == "os":
                    return self._constant_key(node.args[0]) if node.args else None
                if (
                    func.attr == "get"
                    and isinstance(owner, ast.Attribute)
                    and owner.attr == "environ"
                    and isinstance(owner.value, ast.Name)
                    and owner.value.id == "os"
                ):
                    return self._constant_key(node.args[0]) if node.args else None
            if isinstance(func, ast.Name):
                origin = module.imports.get(func.id)
                if origin == ("os", "getenv"):
                    return self._constant_key(node.args[0]) if node.args else None
                if func.id in ("getenv", "env") and not origin:
                    return self._constant_key(node.args[0]) if node.args else None
        return None

    def _settings_key(self, node: ast.expr) -> str | None:
        name = self._dotted_name(node)
        if not name:
            return None
        root = name.split(".", 1)[0]
        if root in _SETTINGS_ROOTS:
            return name
        origin = self.scope.module.imports.get(root)
        if origin and any(
            token in origin[0].lower() for token in ("settings", "config", "conf")
        ):
            return name
        return None

    def _dotted_name(self, node: ast.expr) -> str | None:
        return _dotted_name(node)

    def _constant_key(self, node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            return value or None
        return None


# ───────────────────────── 调用识别 ─────────────────────────


def _call_identity(
    func: ast.expr,
    scope: _Scope,
) -> tuple[str, str] | None:
    """返回 (library, action)；action 为 HTTP 动词、`request` 或 `urlopen`。"""
    imports = scope.module.imports

    if isinstance(func, ast.Name):
        origin = imports.get(func.id)
        if origin:
            module, name = origin
            if module in ("requests", "httpx") and (name in _HTTP_VERBS or name == "request"):
                return module, name
            if module.startswith("urllib") and name == "urlopen":
                return "urllib", "urlopen"
        client = scope.clients.get(func.id)
        if client:
            return client, "request"
        return None

    if isinstance(func, ast.Attribute):
        if func.attr == "urlopen":
            owner = _dotted_name(func.value)
            if owner in ("urllib.request", "urllib"):
                return "urllib", "urlopen"
        base = func.value
        if isinstance(base, ast.Name):
            client = scope.clients.get(base.id)
            if client and (func.attr in _HTTP_VERBS or func.attr == "request"):
                return client, func.attr
            origin = imports.get(base.id)
            if origin:
                module, name = origin
                if not name and module in ("requests", "httpx"):
                    if func.attr in _HTTP_VERBS or func.attr == "request":
                        return module, func.attr
                if module == "urllib.request" and func.attr == "urlopen":
                    return "urllib", "urlopen"
                if module == "urllib" and name == "request" and func.attr == "urlopen":
                    return "urllib", "urlopen"
        library = _client_library(base, scope)
        if library and (func.attr in _HTTP_VERBS or func.attr == "request"):
            return library, func.attr
    return None


def _client_library(node: ast.expr, scope: _Scope) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        origin = scope.module.imports.get(func.id)
        if origin and origin in _CLIENT_FACTORIES:
            return origin[0]
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        origin = scope.module.imports.get(func.value.id)
        if origin and not origin[1]:
            if (origin[0], func.attr) in _CLIENT_FACTORIES:
                return origin[0]
    return None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for item in node.keywords:
        if item.arg == name:
            return item.value
    return None


def _request_args(node: ast.Call, action: str) -> tuple[ast.expr | None, ast.expr | None]:
    """返回 (method 表达式, URL 表达式)。"""
    args = list(node.args)
    if action in _HTTP_VERBS:
        return None, (args[0] if args else _keyword(node, "url"))
    if action == "request":
        method = args[0] if args else _keyword(node, "method")
        url = args[1] if len(args) > 1 else _keyword(node, "url")
        return method, url
    if action == "urlopen":
        return None, (args[0] if args else _keyword(node, "url"))
    return None, None


def _urllib_request_target(node: ast.expr | None) -> tuple[str | None, ast.expr | None]:
    """`urlopen(Request(url, method=...))` → (method, url 表达式)。"""
    if isinstance(node, ast.Call):
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name == "Request":
            args = list(node.args)
            url = args[0] if args else _keyword(node, "url")
            method_expr = args[1] if len(args) > 1 else _keyword(node, "method")
            method = None
            if isinstance(method_expr, ast.Constant) and isinstance(method_expr.value, str):
                method = method_expr.value.upper() or None
            return method, url
    return None, node


def _literal_method(node: ast.expr | None, scope: _Scope, seen: frozenset[str] = frozenset()) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return normalize_method(node.value)
    if isinstance(node, ast.Name) and node.id not in seen:
        assignments = scope.vars.get(node.id) or []
        values = {
            _literal_method(expr, scope, seen | {node.id}) for expr in assignments[-4:]
        }
        values.discard("")
        if len(values) == 1:
            return next(iter(values))
    return ""


# ───────────────────────── 源码索引 ─────────────────────────


class _BindingVisitor(ast.NodeVisitor):
    """收集当前作用域的赋值（不进入嵌套函数/类）。"""

    def __init__(self, module: _Module) -> None:
        self.module = module
        self.vars: dict[str, list[ast.expr]] = {}
        self.clients: dict[str, str] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def _record(self, target: ast.expr, value: ast.expr | None) -> None:
        if value is None:
            return
        if isinstance(target, ast.Name):
            self.vars.setdefault(target.id, []).append(value)
            library = _client_library(value, _Scope(self.module))
            if library:
                self.clients[target.id] = library
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._record(item, None)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            self._record(target, node.value)
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None:
            self._record(node.target, node.value)
            self.generic_visit(node.value)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        for item in node.items:
            if item.optional_vars is not None:
                self._record(item.optional_vars, item.context_expr)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self.visit_With(node)


def _module_imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    imports: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".", 1)[0]] = (alias.name, "")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports[alias.asname or alias.name] = (module, alias.name)
    return imports


def _module_bindings(module: _Module) -> tuple[dict[str, list[ast.expr]], dict[str, str]]:
    visitor = _BindingVisitor(module)
    for node in module.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        visitor.visit(node)
    return visitor.vars, visitor.clients


def _function_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef, module: _Module
) -> tuple[dict[str, list[ast.expr]], dict[str, str]]:
    visitor = _BindingVisitor(module)
    for item in node.body:
        visitor.visit(item)
    return visitor.vars, visitor.clients


def _functions_in_module(module: _Module) -> list[_Function]:
    result: list[_Function] = []

    def walk(nodes: list[ast.stmt], prefix: str, class_name: str, depth: int) -> None:
        if depth > 6 or len(result) >= MAX_FUNCTIONS_PER_FILE:
            return
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}.", node.name, depth + 1)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{node.name}"
                result.append(
                    _Function(
                        module=module,
                        node=node,
                        name=node.name,
                        qualname=qualname,
                        class_name=class_name,
                    )
                )
                walk(node.body, f"{qualname}.", class_name, depth + 1)
            elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                walk(getattr(node, "body", []), prefix, class_name, depth + 1)
                walk(getattr(node, "orelse", []), prefix, class_name, depth + 1)
                for handler in getattr(node, "handlers", []):
                    walk(handler.body, prefix, class_name, depth + 1)

    walk(module.tree.body, "", "", 0)
    return result


_JAVA_METHOD = re.compile(
    r"(?m)^[ \t]*(?:(?:public|private|protected|static|final|synchronized|"
    r"abstract|default|native|strictfp|override|suspend|inline)\s+)*"
    r"[\w<>\[\],.?@\s]+\s+(\w+)\s*\([^;{}]*\)\s*(?:throws[^{]+)?\{"
)
_GO_FUNC = re.compile(
    r"(?m)^func\s+(?:\(\s*\w+\s+\*?\s*(\w+)(?:\[[^\]]*\])?\s*\)\s*)?(\w+)\s*\("
)
_NODE_FUNCTIONS = (
    re.compile(r"\bfunction\s+(\w+)\s*\("),
    re.compile(
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?"
        r"(?:\([^)]*\)|\w+)\s*=>"
    ),
    re.compile(r"(?m)^[ \t]*(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::[^{]+)?\{"),
)


def _language_symbols(
    text: str, *, rel: str, path: Path, language: str
) -> list[_LangSymbol]:
    symbols: list[_LangSymbol] = []
    if language == "java":
        classes = list(
            re.finditer(r"\b(?:class|interface|enum|record|object)\s+(\w+)", text)
        )
        for class_match in classes:
            owner = class_match.group(1)
            brace = text.find("{", class_match.end())
            if brace < 0:
                continue
            end = _matching_brace(text, brace)
            body = text[brace + 1 : end]
            for match in _JAVA_METHOD.finditer(body):
                name = match.group(1)
                if name in _NOT_CALLEES or name == owner:
                    continue
                symbols.append(
                    _LangSymbol(
                        path=path,
                        rel=rel,
                        language=language,
                        name=name,
                        owner=owner,
                        line=_line_of(text, brace + 1 + match.start()),
                    )
                )
    elif language == "go":
        for match in _GO_FUNC.finditer(text):
            owner = match.group(1) or ""
            name = match.group(2)
            if name in _NOT_CALLEES:
                continue
            symbols.append(
                _LangSymbol(
                    path=path,
                    rel=rel,
                    language=language,
                    name=name,
                    owner=owner,
                    line=_line_of(text, match.start()),
                )
            )
    else:
        for pattern in _NODE_FUNCTIONS:
            for match in pattern.finditer(text):
                name = match.group(1)
                if name in _NOT_CALLEES:
                    continue
                symbols.append(
                    _LangSymbol(
                        path=path,
                        rel=rel,
                        language=language,
                        name=name,
                        owner="",
                        line=_line_of(text, match.start()),
                    )
                )
    return symbols


def _candidate_names(name: str) -> list[str]:
    clean = str(name or "").strip()
    if not clean:
        return []
    parts = [part for part in _NAME_SPLIT.split(clean) if part]
    last = clean.rsplit(".", 1)[-1]
    names = [clean, last]
    if parts:
        names.append(parts[-1])
        if len(parts) >= 2:
            names.append(f"{parts[-2]}.{parts[-1]}")
    result: list[str] = []
    for item in names:
        if item and item not in result:
            result.append(item)
    return result


class DownstreamScanner:
    """项目级、可复用的 Python 出站 HTTP 调用点扫描器。"""

    def __init__(
        self,
        project_root: str | Path,
        *,
        codegraph_client: Any | None = None,
        max_symbols: int = MAX_SYMBOLS,
        max_depth: int = MAX_DEPTH,
        max_call_sites: int = MAX_CALL_SITES,
        max_files: int = MAX_FILES,
    ):
        self.root = Path(project_root).resolve()
        self.codegraph = codegraph_client
        self.max_symbols = max(1, min(int(max_symbols or MAX_SYMBOLS), MAX_SYMBOLS))
        self.max_depth = max(0, min(int(max_depth), MAX_DEPTH))
        self.max_call_sites = max(1, min(int(max_call_sites), MAX_CALL_SITES))
        self.max_files = max(1, min(int(max_files), MAX_FILES))
        self.warnings: list[str] = []
        self._modules: dict[Path, _Module | None] = {}
        self._symbol_index: dict[str, list[_Function]] | None = None
        self._all_functions: list[_Function] = []
        self._file_order: list[Path] | None = None
        self._lang_file_order: list[tuple[Path, str]] | None = None
        self._lang_index: dict[str, list[_LangSymbol]] | None = None
        self._lang_texts: dict[Path, str] = {}
        self._lang_resolve_cache: dict[
            tuple[str, str, int], list[_LangSymbol]
        ] = {}
        self._scan_cache: dict[str, DownstreamScan] = {}
        self._call_site_cache: dict[
            int, tuple[list[HttpCallSite], list[str], dict[str, str]]
        ] = {}
        self._language_checked = False
        self._supported_languages: set[str] | None = None

    def _check_language(self) -> bool:
        """图谱声明支持语言时，只扫描其声明的语言；全部不支持则跳过。"""
        if self._language_checked:
            return bool(self._supported_languages is None or self._supported_languages)
        self._language_checked = True
        if self.codegraph is None:
            return True
        try:
            languages = self.codegraph.supported_languages() or []
        except Exception:
            languages = []
        if not languages:
            return True
        supported = {
            _LANGUAGE_ALIASES[str(item).lower()]
            for item in languages
            if str(item).lower() in _LANGUAGE_ALIASES
        }
        if not supported:
            self._supported_languages = set()
            self.warnings.append(
                "downstream_language_not_supported:" + ",".join(map(str, languages))
            )
            return False
        self._supported_languages = supported
        return True

    def _language_allowed(self, language: str) -> bool:
        if self._supported_languages is not None and self._supported_languages:
            return language in self._supported_languages
        return True

    # ---- 文件与索引 ----

    def _walk_source_files(self) -> list[Path]:
        if self._file_order is not None:
            return self._file_order
        result: list[Path] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SKIP_DIRS and not name.startswith(".")
            )
            for filename in sorted(filenames):
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
                        f"source_skipped_large:{path.relative_to(self.root)}"
                    )
                    continue
                if total + size > MAX_TOTAL_BYTES:
                    self.warnings.append("source_scan_truncated_total_bytes")
                    self._file_order = result
                    return result
                total += size
                result.append(resolved)
                if len(result) >= self.max_files:
                    self.warnings.append("source_scan_truncated_file_count")
                    self._file_order = result
                    return result
        self._file_order = result
        return result

    def _files(self) -> list[Path]:
        return [path for path in self._walk_source_files() if path.suffix == ".py"]

    def _language_files(self) -> list[tuple[Path, str]]:
        if self._lang_file_order is not None:
            return self._lang_file_order
        result: list[tuple[Path, str]] = []
        for path in self._walk_source_files():
            language = language_for(path)
            if language and self._language_allowed(language):
                result.append((path, language))
        self._lang_file_order = result
        return result

    def _language_text(self, path: Path) -> str:
        cached = self._lang_texts.get(path)
        if cached is not None:
            return cached
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        self._lang_texts[path] = text
        return text

    def _module(self, path: Path) -> _Module | None:
        if path in self._modules:
            return self._modules[path]
        module: _Module | None = None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text, filename=str(path))
            rel = path.relative_to(self.root).as_posix()
            module = _Module(path=path, rel=rel, tree=tree)
            module.imports = _module_imports(tree)
            module.vars, module.clients = _module_bindings(module)
            module.functions = _functions_in_module(module)
        except Exception as exc:  # 单个源码文件失败不影响其余文件
            self.warnings.append(f"source_parse_error:{path.name}:{type(exc).__name__}")
            module = None
        self._modules[path] = module
        return module

    def _index(self) -> dict[str, list[_Function]]:
        if self._symbol_index is not None:
            return self._symbol_index
        index: dict[str, list[_Function]] = {}
        for path in self._files():
            module = self._module(path)
            if module is None:
                continue
            for function in module.functions:
                self._all_functions.append(function)
                index.setdefault(function.name, []).append(function)
                index.setdefault(function.qualname, []).append(function)
        self._symbol_index = index
        return index

    def _language_index(self) -> dict[str, list[_LangSymbol]]:
        if self._lang_index is not None:
            return self._lang_index
        index: dict[str, list[_LangSymbol]] = {}
        for path, language in self._language_files():
            text = self._language_text(path)
            if not text.strip():
                continue
            rel = path.relative_to(self.root).as_posix()
            for symbol in _language_symbols(
                text, rel=rel, path=path, language=language
            ):
                keys = [symbol.name]
                if symbol.owner:
                    keys.append(f"{symbol.owner}.{symbol.name}")
                for key in keys:
                    index.setdefault(key, []).append(symbol)
        self._lang_index = index
        return index

    def _resolve_lang(
        self,
        name: str,
        *,
        file_hint: str = "",
        line_hint: int = 0,
    ) -> list[_LangSymbol]:
        clean = str(name or "").strip()
        if not clean:
            # 内联回调（Node 箭头函数等）没有符号名，只能靠路由给出的文件+行定位。
            if not file_hint:
                return []
            fallback = self._lang_file_hint(file_hint)
            if fallback is None:
                return []
            path, language = fallback
            return [
                _LangSymbol(
                    path=path,
                    rel=path.relative_to(self.root).as_posix(),
                    language=language,
                    name="",
                    owner="",
                    line=int(line_hint or 0),
                )
            ]
        cache_key = (clean, str(file_hint or ""), int(line_hint or 0))
        cached = self._lang_resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        index = self._language_index()
        candidates: list[_LangSymbol] = []
        for key in _candidate_names(clean):
            candidates.extend(index.get(key, []))
        unique: dict[tuple[str, int], _LangSymbol] = {}
        for symbol in candidates:
            unique[(symbol.rel, symbol.line)] = symbol
        candidates = list(unique.values())

        if file_hint:
            hinted = [
                symbol
                for symbol in candidates
                if self._file_matches(symbol.path, file_hint)
            ]
            if hinted:
                candidates = hinted
            elif not candidates:
                # 路由指向的文件里没索引到方法时，直接按行号定位函数块。
                fallback = self._lang_file_hint(file_hint)
                if fallback is not None:
                    path, language = fallback
                    rel = path.relative_to(self.root).as_posix()
                    simple = (
                        _candidate_names(clean)[-1] if _candidate_names(clean) else clean
                    )
                    candidates = [
                        _LangSymbol(
                            path=path,
                            rel=rel,
                            language=language,
                            name=simple,
                            owner="",
                            line=int(line_hint or 0),
                        )
                    ]

        same_file = (
            [symbol for symbol in candidates if self._file_matches(symbol.path, file_hint)]
            if file_hint
            else list(candidates)
        )
        # 行号只在同一文件内有意义；路由文件与 handler 文件不同时不拿行号消歧。
        if line_hint and same_file and (not file_hint or same_file):
            inside = [
                symbol
                for symbol in same_file
                if symbol.line and abs(symbol.line - line_hint) <= 3
            ]
            if inside:
                candidates = inside
            else:
                before = [symbol for symbol in same_file if symbol.line <= line_hint]
                if before:
                    candidates = [max(before, key=lambda item: item.line)]
                else:
                    candidates = [min(same_file, key=lambda item: item.line)]

        if len({(item.rel, item.line) for item in candidates}) > 1:
            self.warnings.append(f"symbol_ambiguous:{clean}")
            candidates = []
        self._lang_resolve_cache[cache_key] = candidates
        return candidates

    def _lang_file_hint(self, hint: str) -> tuple[Path, str] | None:
        for path, language in self._language_files():
            if self._file_matches(path, hint):
                return path, language
        return None

    def _resolve(
        self,
        name: str,
        *,
        file_hint: str = "",
        line_hint: int = 0,
    ) -> list[_Function]:
        clean = str(name or "").strip()
        if not clean:
            return []
        if "::" in clean or "<" in clean:
            return []  # Java/其他语言符号，不按 Python 猜
        index = self._index()
        candidates: list[_Function] = []
        for key in (clean, clean.rsplit(".", 1)[-1]):
            candidates.extend(index.get(key, []))
        # module.Class.method 形式：按 qualname 后缀匹配
        if "." in clean:
            for function in self._all_functions:
                if clean.endswith("." + function.qualname):
                    candidates.append(function)
        unique: dict[tuple[str, int], _Function] = {}
        for function in candidates:
            unique[(function.module.rel, function.node.lineno)] = function
        candidates = list(unique.values())
        if not candidates:
            return []

        if file_hint:
            hinted = [
                function
                for function in candidates
                if self._file_matches(function.module.path, file_hint)
            ]
            if hinted:
                candidates = hinted
        if line_hint:
            in_line = [
                function
                for function in candidates
                if function.node.lineno
                <= line_hint
                <= (function.node.end_lineno or function.node.lineno)
            ]
            if in_line:
                candidates = in_line
        if len(candidates) > 1:
            # 仍重名：没有文件/行号证据时不把调用点挂到任意一个函数。
            self.warnings.append(f"symbol_ambiguous:{clean}")
            return []
        return candidates

    def _file_matches(self, path: Path, hint: str) -> bool:
        candidate = Path(hint)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            return candidate.resolve() == path
        except (OSError, ValueError):
            return path.as_posix().endswith(str(hint).replace("\\", "/"))

    # ---- 函数扫描 ----

    def _scan_function(
        self, function: _Function
    ) -> tuple[list[HttpCallSite], list[str], dict[str, str]]:
        key = id(function.node)
        if key in self._call_site_cache:
            return self._call_site_cache[key]

        module = function.module
        scope = _Scope(
            module=module,
            vars={name: list(exprs) for name, exprs in module.vars.items()},
            clients=dict(module.clients),
        )
        local_vars, local_clients = _function_bindings(function.node, module)
        for name, exprs in local_vars.items():
            scope.vars.setdefault(name, []).extend(exprs)
        scope.clients.update(local_clients)

        evaluator = _ExpressionEvaluator(scope, function.qualname)
        visitor = _CallVisitor(self, function, scope, evaluator)
        for item in function.node.body:
            visitor.visit(item)

        result = (visitor.sites, visitor.local_callees, visitor.unsupported_reasons)
        self._call_site_cache[key] = result
        return result

    # ---- 对外入口 ----

    def scan(
        self,
        handler: str,
        *,
        owner: str = "",
        file_hint: str = "",
        line_hint: int = 0,
    ) -> DownstreamScan:
        cache_key = "|".join(
            (str(owner), str(handler), str(file_hint), str(line_hint))
        )
        cached = self._scan_cache.get(cache_key)
        if cached is not None:
            return cached

        if not self._check_language():
            result = DownstreamScan(handler=handler)
            result.warnings = _dedupe(list(self.warnings))
            self._scan_cache[cache_key] = result
            return result

        result = DownstreamScan(handler=handler)
        seeds = [handler]
        if owner and handler:
            seeds.insert(0, f"{owner}.{handler}")
        queue: list[tuple[str, int, str, int]] = [
            (seed, 0, file_hint, line_hint) for seed in seeds
        ]
        visited: set[tuple[str, int]] = set()
        all_sites: list[HttpCallSite] = []
        unsupported_reasons: dict[str, str] = {}
        warnings: list[str] = []

        queue_cap = self.max_symbols * 4
        while queue and len(visited) < self.max_symbols and len(all_sites) < self.max_call_sites:
            if len(queue) > queue_cap:
                del queue[queue_cap:]
                warnings.append("downstream_queue_truncated")
            name, depth, file_hint, line_hint = queue.pop(0)
            functions = self._resolve(name, file_hint=file_hint, line_hint=line_hint)
            if not functions:
                lang_symbols = self._resolve_lang(
                    name, file_hint=file_hint, line_hint=line_hint
                )
                if not lang_symbols:
                    if depth == 0:
                        warnings.append(f"handler_source_missing:{name}")
                    continue
                for symbol in lang_symbols:
                    key = (symbol.rel, symbol.line or 0)
                    if key in visited:
                        continue
                    if len(visited) >= self.max_symbols:
                        warnings.append("downstream_symbol_limit")
                        break
                    visited.add(key)
                    result.checked = True
                    lang_scan = scan_language_file(
                        path=symbol.path,
                        rel=symbol.rel,
                        language=symbol.language,
                        handler=symbol.name,
                        line_hint=symbol.line or line_hint,
                        file_text=self._language_text(symbol.path),
                        max_sites=self.max_call_sites,
                    )
                    all_sites.extend(lang_scan.sites)
                    unsupported_reasons.update(lang_scan.unsupported_reasons)
                    warnings.extend(lang_scan.warnings)
                    if depth >= self.max_depth:
                        continue
                    for callee in lang_scan.local_callees:
                        if callee in (handler, f"{owner}.{handler}", symbol.name):
                            continue
                        queue.append((callee, depth + 1, symbol.rel, 0))
                    self._queue_codegraph_callees(
                        name, depth, queue, warnings
                    )
                continue
            for function in functions:
                key = (function.module.rel, function.node.lineno)
                if key in visited:
                    continue
                if len(visited) >= self.max_symbols:
                    warnings.append("downstream_symbol_limit")
                    break
                visited.add(key)
                result.checked = True
                sites, callees, reasons = self._scan_function(function)
                all_sites.extend(sites)
                unsupported_reasons.update(reasons)
                if depth >= self.max_depth:
                    continue
                for callee in callees:
                    if callee in (handler, f"{owner}.{handler}"):
                        continue
                    queue.append(
                        (callee, depth + 1, function.module.rel, 0)
                    )
                self._queue_codegraph_callees(name, depth, queue, warnings)

        # 同一物理调用点可能同时从 handler 和 callee 到达；按 id 去重。
        unique_sites: dict[str, HttpCallSite] = {}
        for site in all_sites:
            unique_sites.setdefault(site.call_site_id, site)
        ordered_sites = sorted(
            unique_sites.values(), key=lambda item: (item.file, item.line, item.call_site_id)
        )[: self.max_call_sites]

        result.call_sites = ordered_sites
        if not result.checked and handler:
            warnings.append("downstream_unmapped:no_source_function")
        self._finalize(result, warnings, unsupported_reasons)
        self._scan_cache[cache_key] = result
        return result

    def _queue_codegraph_callees(
        self,
        name: str,
        depth: int,
        queue: list[tuple[str, int, str, int]],
        warnings: list[str],
    ) -> None:
        if self.codegraph is None:
            return
        try:
            symbols = self.codegraph.callees(name) or []
        except Exception as exc:  # 图谱是辅助证据，失败不中断
            warnings.append(
                f"codegraph_callees_error:{type(exc).__name__}:{str(exc)[:120]}"
            )
            return
        for symbol in symbols[: self.max_symbols]:
            symbol_name = getattr(symbol, "name", "") or str(symbol)
            if not symbol_name:
                continue
            queue.append(
                (
                    symbol_name,
                    depth + 1,
                    getattr(symbol, "file", "") or "",
                    int(getattr(symbol, "line", 0) or 0),
                )
            )

    def _finalize(
        self,
        result: DownstreamScan,
        warnings: list[str],
        unsupported_reasons: dict[str, str] | None = None,
    ) -> None:
        sites_by_id = {site.call_site_id: site for site in result.call_sites}
        plan = build_dep_id_plan(
            [
                CallSite(
                    call_site_id=site.call_site_id,
                    method=site.method,
                    target=site.target,
                    path=site.path,
                    service_name=site.service_name,
                    config_key=site.config_key,
                )
                for site in result.call_sites
            ]
        )

        result.deps = []
        for dep in plan.deps:
            details = [sites_by_id.get(item) for item in dep.call_sites]
            result.deps.append(
                {
                    "dep_id": dep.dep_id,
                    "dep_type": "http",
                    "evidence": dep.evidence,
                    "method": dep.method,
                    "path_key": dep.path_key,
                    "target": dep.target,
                    "targets": list(dep.targets),
                    "route_host": dep.route_host,
                    "call_sites": list(dep.call_sites),
                    "inferred": dep.inferred,
                    "config_keys": sorted(
                        {
                            site.config_key
                            for site in details
                            if site is not None and site.config_key
                        }
                    ),
                }
            )

        reasons = unsupported_reasons or {}
        result.unsupported = []
        for item in plan.unsupported:
            site = sites_by_id.get(item.call_site_id)
            detail = item.detail or reasons.get(item.call_site_id, "")
            result.unsupported.append(
                {
                    "call_site_id": item.call_site_id,
                    "reason": item.reason,
                    "detail": detail,
                    "file": site.file if site else "",
                    "line": site.line if site else 0,
                    "method": site.method if site else "",
                    "target": site.target if site else None,
                    "path": site.path if site else None,
                    "config_key": site.config_key if site else None,
                }
            )

        result.collisions = [
            {
                "dep_id": item.dep_id,
                "targets": list(item.targets),
                "resolved": item.resolved,
                "from_id": item.from_id,
                "to_id": item.to_id,
            }
            for item in plan.collisions
        ]
        result.warnings = _dedupe([*warnings, *plan.warnings, *self.warnings])


class _CallVisitor(ast.NodeVisitor):
    """在一个函数体内收集 HTTP 调用点与本地 callee 名。"""

    def __init__(
        self,
        scanner: DownstreamScanner,
        function: _Function,
        scope: _Scope,
        evaluator: _ExpressionEvaluator,
    ):
        self.scanner = scanner
        self.function = function
        self.scope = scope
        self.evaluator = evaluator
        self.sites: list[HttpCallSite] = []
        self.local_callees: list[str] = []
        self.unsupported_reasons: dict[str, str] = {}
        self._root = function.node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is not self._root:
            self._note_callee(node.name)
            return
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        if node is not self._root:
            self._note_callee(node.name)
            return
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        identity = _call_identity(node.func, self.scope)
        if identity is None:
            self._note_callee_from_expr(node.func)
            self.generic_visit(node)
            return
        library, action = identity
        self._record(node, library, action)
        self.generic_visit(node)

    def _record(self, node: ast.Call, library: str, action: str) -> None:
        method_expr, url_expr = _request_args(node, action)
        method = ""
        if action in _HTTP_VERBS:
            method = action.upper()
        elif action == "request":
            method = _literal_method(method_expr, self.scope)
        elif action == "urlopen":
            method, url_expr = _urllib_request_target(url_expr)
            method = method or "GET"
        else:
            return

        call_site_id = (
            f"{self.function.module.rel}:{node.lineno}:{node.col_offset}"
        )
        if url_expr is None:
            self._record_unsupported(
                call_site_id, node, method, "url_missing", None
            )
            return

        evidence = self.evaluator.evaluate(url_expr)
        target = evidence.target if evidence.kind == "target" else None
        path = evidence.path if evidence.kind in ("path", "config", "dynamic") else None
        if evidence.kind == "config" and not path:
            path = None
        if target is None and path is None:
            self._record_unsupported(
                call_site_id,
                node,
                method,
                evidence.detail or "target_unresolved",
                evidence.config_key,
            )
            return

        self.sites.append(
            HttpCallSite(
                call_site_id=call_site_id,
                handler=self.function.qualname,
                file=self.function.module.rel,
                line=node.lineno,
                method=method,
                target=target,
                path=path,
                config_key=evidence.config_key,
                client=library,
            )
        )

    def _record_unsupported(
        self,
        call_site_id: str,
        node: ast.Call,
        method: str,
        reason: str,
        config_key: str | None,
    ) -> None:
        # 统一在 _finalize 里转成 unsupported；这里用空 path/target 的 CallSite
        # 交给 build_dep_id_plan，保留 method/位置证据。
        self.unsupported_reasons[call_site_id] = reason
        self.sites.append(
            HttpCallSite(
                call_site_id=call_site_id,
                handler=self.function.qualname,
                file=self.function.module.rel,
                line=node.lineno,
                method=method,
                target=None,
                path=None,
                config_key=config_key,
                client="",
            )
        )

    def _note_callee_from_expr(self, expr: ast.expr) -> None:
        if isinstance(expr, ast.Name):
            self._note_callee(expr.id)
        elif isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in ("self", "cls"):
                if self.function.class_name:
                    self._note_callee(f"{self.function.class_name}.{expr.attr}")
                else:
                    self._note_callee(expr.attr)
            elif isinstance(expr.value, ast.Name):
                origin = self.scope.module.imports.get(expr.value.id)
                if origin and not origin[1]:
                    self._note_callee(f"{origin[0]}.{expr.attr}")

    def _note_callee(self, name: str) -> None:
        if name and name not in self.local_callees:
            self.local_callees.append(name)


# ───────────────────────── 计划草案 ─────────────────────────


def build_plan_from_profiles(
    profiles: list[dict[str, Any]],
    *,
    sut_id: str,
    host: str = "127.0.0.1",
    port: int = 0,
) -> dict[str, Any]:
    """把 C3 画像中的下游依赖汇总成 `MockPlan` 草案。

    草案只写能静态证明的内容：

    - 路由：`dep_id` + 规范化 method/path/host/target，**分支留空**由人工补语义；
    - 有配置键的 dep：注入标 `unsupported`，原因 `injection_original_unknown`
      （原值必须由人工从 SUT 配置补全，不伪造）；
    - 有确定目标的 dep：用 `http_proxy`（不依赖配置键）；
    - 其余：`unsupported` / `target_unresolved`。
    """
    merged: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        for dep in profile.get("downstream_deps") or []:
            dep_id = str(dep.get("dep_id") or "")
            if not dep_id:
                continue
            current = merged.get(dep_id)
            if current is None:
                current = {
                    "dep_id": dep_id,
                    "method": str(dep.get("method") or "").upper(),
                    "path_key": str(dep.get("path_key") or "/"),
                    "host": dep.get("route_host"),
                    "target": dep.get("target"),
                    "call_sites": [],
                    "config_keys": [],
                }
                merged[dep_id] = current
            for item in dep.get("call_sites") or []:
                if item not in current["call_sites"]:
                    current["call_sites"].append(item)
            for key in dep.get("config_keys") or []:
                if key not in current["config_keys"]:
                    current["config_keys"].append(key)
            if not current["target"] and dep.get("target"):
                current["target"] = dep["target"]
            if not current["host"] and dep.get("route_host"):
                current["host"] = dep["route_host"]

    routes: list[dict[str, Any]] = []
    injections: list[dict[str, Any]] = []
    for dep_id in sorted(merged):
        dep = merged[dep_id]
        routes.append(
            {
                "dep_id": dep_id,
                "method": dep["method"],
                "path_key": dep["path_key"],
                "host": dep["host"],
                "target": dep["target"],
                "source": "c3",
                "branches": {},
            }
        )
        config_keys = sorted(dep["config_keys"])
        if config_keys:
            injections.append(
                {
                    "dep_id": dep_id,
                    "strategy": "unsupported",
                    "kind": "base_url",
                    "property": config_keys[0],
                    "reason": "injection_original_unknown",
                }
            )
        elif dep["target"]:
            injections.append(
                {
                    "dep_id": dep_id,
                    "strategy": "http_proxy",
                    "kind": "proxy",
                    "path_mode": "append",
                }
            )
        else:
            injections.append(
                {
                    "dep_id": dep_id,
                    "strategy": "unsupported",
                    "kind": "base_url",
                    "reason": "target_unresolved",
                }
            )

    return {
        "schema_version": 1,
        "sut_id": sut_id,
        "namespace": sut_id,
        "host": host,
        "port": int(port),
        "capture_bodies": False,
        "routes": routes,
        "injections": injections,
    }


def scan_downstream(
    project_root: str | Path,
    handler: str,
    *,
    owner: str = "",
    codegraph_client: Any | None = None,
) -> DownstreamScan:
    """单次扫描便捷入口；批量扫描应复用 `DownstreamScanner`。"""
    scanner = DownstreamScanner(project_root, codegraph_client=codegraph_client)
    return scanner.scan(handler, owner=owner)


__all__ = [
    "DownstreamScan",
    "DownstreamScanner",
    "HttpCallSite",
    "build_plan_from_profiles",
    "scan_downstream",
]
