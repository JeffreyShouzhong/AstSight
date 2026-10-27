"""路由前缀重建（Route Prefix Repair）。

## 为什么需要这个模块

实测（2026-10-25，`@colbymchenry/codegraph` 1.6.2）发现 CodeGraph 的路由产出
有三个**静默缺陷**——不报错，只给出错误结果：

1. **Flask Blueprint 的 `url_prefix` 不拼接**
   `Blueprint("orders", __name__, url_prefix="/api/v1")` + `@bp.route("/orders")`
   → 图谱给出 `/orders`，真实路径是 `/api/v1/orders`。
2. **Django `include()` 的前缀不拼接**
   `path("api/v1/", include("shop.urls"))` + `path("orders/", ...)`
   → 图谱给出 `orders/`，真实路径是 `/api/v1/orders/`。
3. **Django 完全没有 HTTP method**
   图谱路线名是 `orders/`，method 在 view 内部按 `request.method` 分支，
   静态图拿不到。

前两条是**静默错误**，比漏报危险得多：测试会打到缺前缀的错误 URL，得到 404，
然后把失败归因到无关模块。这正是"测试假绿/假红"的来源，必须由框架自己修。

## 修法：确定性交叉校验，不做无脑覆盖

用 AST 解析路由声明，重建完整路径，再与图谱给的路径做**后缀校验**：

- 图谱路径 == 重建路径            → `verified`（两路独立一致）
- 图谱路径是重建路径的后缀        → `verified`，采用重建路径（前缀被补上）
- 图谱路径与重建路径无关          → `unmapped`，两路冲突，**不猜**
- 只有图谱、无重建                → `inferred`（图谱单方证据）
- 只有重建、无图谱                → `verified`，`source=framework_plugin`

"宁可漏、不可错"：冲突时保留双方证据并标 `unmapped`，交由人工或 LLM 复核，
绝不用"看起来更像"来选边。

## 边界

- 仅 Python（Django / Flask / FastAPI）。Java/Spring 走插件接口，另行实现。
- 不做运行时反射：AST 拿不到的（动态注册、装饰器工厂、元类生成）一律不猜。
- 不改变路径参数语法：`<int:order_id>` 原样保留，规范化由调用方决定。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from .client import INFERRED, UNMAPPED, VERIFIED, RouteRef

# 扫描时跳过的目录：索引产物、依赖、构建输出与虚拟环境
_SKIP_DIRS = {
    ".git", ".codegraph", ".astsight", ".venv", "venv", "env", "node_modules",
    "__pycache__", "build", "dist", ".tox", ".mypy_cache", ".pytest_cache",
    "site-packages", "migrations",
}

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_DJANGO_ROUTE_FUNCS = {"path", "re_path", "url"}

# DRF ViewSet/Mixin → 动作 → HTTP method。只展开静态可知的 CRUD 动作；
# 自定义 @action 另行处理。展开结果标 inferred，因为它来自框架约定而非显式声明。
_DRF_ACTION_METHODS = {
    "list": "GET",
    "create": "POST",
    "retrieve": "GET",
    "update": "PUT",
    "partial_update": "PATCH",
    "destroy": "DELETE",
}
_DRF_MIXIN_ACTIONS = {
    "ListModelMixin": ("list",),
    "CreateModelMixin": ("create",),
    "RetrieveModelMixin": ("retrieve",),
    "UpdateModelMixin": ("update", "partial_update"),
    "DestroyModelMixin": ("destroy",),
}
_DRF_BASE_ACTIONS = {
    "ModelViewSet": (
        "list", "create", "retrieve", "update", "partial_update", "destroy",
    ),
    "ReadOnlyModelViewSet": ("list", "retrieve"),
}

# DRF 通用视图：HTTP method → 实际执行的 action。类里没显式写 get/post 时，
# 由基类提供 dispatch，静态只能按这个约定展开（仍标 inferred）。
_DRF_GENERIC_VIEW_ACTIONS = {
    "ListAPIView": (("GET", "list"),),
    "CreateAPIView": (("POST", "create"),),
    "ListCreateAPIView": (("GET", "list"), ("POST", "create")),
    "RetrieveAPIView": (("GET", "retrieve"),),
    "RetrieveUpdateAPIView": (
        ("GET", "retrieve"), ("PUT", "update"), ("PATCH", "partial_update"),
    ),
    "RetrieveDestroyAPIView": (("GET", "retrieve"), ("DELETE", "destroy")),
    "RetrieveUpdateDestroyAPIView": (
        ("GET", "retrieve"), ("PUT", "update"),
        ("PATCH", "partial_update"), ("DELETE", "destroy"),
    ),
    "UpdateAPIView": (("PUT", "update"), ("PATCH", "partial_update")),
    "DestroyAPIView": (("DELETE", "destroy"),),
}

_DJANGO_NAMED_GROUP = re.compile(r"\(\?P<([A-Za-z_]\w*)>[^)]*\)")
_DJANGO_INLINE_FLAG = re.compile(r"\(\?[aiLmsux]+\)")


@dataclass(frozen=True)
class RouteDeclaration:
    """从源码重建出的一条路由声明。"""

    framework: str          # django | flask | fastapi
    method: str             # 空串 = 框架不体现 method（Django 常态）
    path: str               # 完整路径，已含全部前缀，以 / 开头
    handler: str
    file: str               # handler 所在文件（相对项目根的 POSIX 路径）
    line: int
    raw_path: str = ""      # 拼接前的局部路径，便于排错
    owner: str = ""         # FastAPI: APIRouter/app 变量名（跨文件 include 用）
    route_file: str = ""    # 路由声明所在文件（URLconf），与 handler 文件可能不同
    handler_owner: str = ""  # 类视图：handler 所属类名
    inferred: bool = False  # 由框架约定/字面量设置推导，而非显式声明
    note: str = ""          # 推导依据，如 drf_router_expanded


@dataclass
class RouteRepairReport:
    """修复统计，进画像与报告，便于审计"修了几条、冲突几条"。"""

    scanned_files: int = 0
    declarations: int = 0
    matched: int = 0
    prefix_repaired: int = 0
    plugin_only: int = 0
    conflicts: int = 0
    graph_only: int = 0
    superseded_graph_routes: int = 0
    unresolved_prefixes: int = 0
    frameworks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned_files": self.scanned_files,
            "declarations": self.declarations,
            "matched": self.matched,
            "prefix_repaired": self.prefix_repaired,
            "plugin_only": self.plugin_only,
            "conflicts": self.conflicts,
            "graph_only": self.graph_only,
            "superseded_graph_routes": self.superseded_graph_routes,
            "unresolved_prefixes": self.unresolved_prefixes,
            "frameworks": list(self.frameworks),
        }


def normalize_django_path(path: str) -> str:
    """把 Django `re_path`/`url` 的正则路径规范化成可读的字面路径。

    CodeGraph 对 Django 路由给出的是原始正则（`^articles/feed/?$`），既不能
    直接请求，也无法与 `path()` 声明的路径比较。这里只做**无损**变换：
    - 去掉首尾锚点 `^` / `$`；
    - 去掉末尾可选斜杠 `/?`；
    - 命名分组 `(?P<slug>...)` → `{slug}`；
    - 去掉内联 flag（`(?i)`）；
    普通分组、字符类等无法还原语义的形式原样保留——不猜。
    """
    text = (path or "").strip()
    if not text:
        return ""
    text = text.replace("\\/", "/")
    if text.startswith("^"):
        text = text[1:]
    if text.endswith("$"):
        text = text[:-1]
    if text.endswith("/?"):
        text = text[:-2]
    text = _DJANGO_NAMED_GROUP.sub(r"{\1}", text)
    text = _DJANGO_INLINE_FLAG.sub("", text)
    while "//" in text:
        text = text.replace("//", "/")
    if text and not text.startswith("/"):
        text = "/" + text
    return text


def _join(*parts: str) -> str:
    """拼接 URL 片段，规整斜杠，始终返回以 / 开头的路径。

    **尾斜杠必须保留**：Django 的 `path("orders/")` 与 `path("orders")` 是两个
    不同 URL（`APPEND_SLASH` 默认对缺斜杠的请求做 301 重定向），Flask 的
    `/orders` 与 `/orders/` 也不会互相兜底。统一 strip 掉尾斜杠会让构造出的
    请求多一跳重定向、甚至直接 404——这是"看起来对、实际错"的典型。
    """
    cleaned: list[str] = []
    for part in parts:
        if not part:
            continue
        cleaned.append(part.strip("/"))
    joined = "/".join(segment for segment in cleaned if segment)
    if not joined:
        return "/"

    last_non_empty = next((p for p in reversed(parts) if p), "")
    trailing = "/" if last_non_empty.endswith("/") else ""
    return "/" + joined + trailing


def _tail(path: str) -> str:
    """去掉查询串/尾斜杠后的比较用形式。"""
    return path.split("?", 1)[0].rstrip("/")


def _module_name_of_file(rel_file: str) -> str:
    """`a/b/urls.py` → a.b.urls；`a/b/__init__.py` → a.b。"""
    rel = rel_file[:-3] if rel_file.endswith(".py") else rel_file
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    elif rel == "__init__":
        rel = ""
    return rel.replace("/", ".")


def _simple_name(node: ast.AST) -> str:
    """Name/Attribute 链的末段名；其它形式返回空串。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


@dataclass
class _ClassInfo:
    """类视图的静态元数据（用于 APIView / DRF ViewSet 展开）。"""

    name: str
    module: str
    file: str
    line: int = 0
    bases: list[str] = field(default_factory=list)
    methods: dict[str, int] = field(default_factory=dict)
    attrs: dict[str, str] = field(default_factory=dict)
    actions: dict[str, dict] = field(default_factory=dict)


@dataclass
class _ProjectMetadata:
    """跨文件静态元数据：类、顶层函数、字面量设置。"""

    modules: set[str] = field(default_factory=set)
    functions: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    classes: dict[tuple[str, str], _ClassInfo] = field(default_factory=dict)
    class_by_name: dict[str, list[_ClassInfo]] = field(default_factory=dict)
    literals: dict[str, set[str]] = field(default_factory=dict)

    def class_info(self, name: str, module_hint: str = "") -> _ClassInfo | None:
        candidates = self.class_by_name.get(name) or []
        if module_hint:
            exact = [item for item in candidates if item.module == module_hint]
            if len(exact) == 1:
                return exact[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def literal(self, name: str) -> str | None:
        values = self.literals.get(name) or set()
        return next(iter(values)) if len(values) == 1 else None


class RoutePrefixRepair:
    """路由前缀重建器。

    典型用法：:

        repair = RoutePrefixRepair(project_root)
        routes = repair.repair(graph_routes)     # 返回修复后的列表
        report = repair.last_report              # 审计统计
    """

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.last_report = RouteRepairReport()

    # ------------------------------------------------------------------ 扫描

    def _iter_py_files(self):
        for path in self.project_root.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in path.relative_to(self.project_root).parts):
                continue
            yield path

    def scan(self) -> list[RouteDeclaration]:
        """扫描全项目的路由声明。

        两遍扫描：第一遍收集跨文件静态元数据（类方法、顶层函数、字面量设置），
        第二遍再做路由展开。FastAPI 的 `module.router` include、Django 的
        `View.as_view()` / DRF `router.register` 都必须先知道目标模块/类，
        单遍 ast.walk 拿不到这些信息。

        单文件解析失败（语法错误、非 UTF-8）直接跳过该文件，不中断整体扫描，
        但在报告里体现扫描文件数，便于发现"扫了但什么都没扫到"的异常。
        两遍都重新读文件而不缓存 AST，避免大仓扫描时把整仓语法树常驻内存。
        """
        declarations: list[RouteDeclaration] = []
        frameworks: set[str] = set()
        scanned = 0
        meta = _ProjectMetadata()

        files: list[tuple[str, Path]] = []
        for path in self._iter_py_files():
            tree = self._read_tree(path)
            if tree is None:
                continue
            scanned += 1
            rel = path.relative_to(self.project_root).as_posix()
            module = _module_name_of_file(rel)
            files.append((rel, path))
            meta.modules.add(module)
            self._collect_metadata(tree, module, rel, meta)

        for rel, path in files:
            tree = self._read_tree(path)
            if tree is None:
                continue
            module = _module_name_of_file(rel)

            if self._looks_like_django(tree):
                frameworks.add("django")
                declarations.extend(
                    self._scan_django_module(tree, rel, module, meta)
                )
            flask = self._scan_flask_module(tree, rel)
            if flask:
                frameworks.add("flask")
                declarations.extend(flask)
            fastapi = self._scan_fastapi_module(tree, rel, module, meta)
            if fastapi:
                frameworks.add("fastapi")
                declarations.extend(fastapi)

        # 按框架分别做跨文件前缀展开（include / include_router）
        if "django" in frameworks:
            django = [d for d in declarations if d.framework == "django"]
            rest = [d for d in declarations if d.framework != "django"]
            declarations = self._resolve_django_includes(django) + rest
        if "fastapi" in frameworks:
            fastapi = [d for d in declarations if d.framework == "fastapi"]
            rest = [d for d in declarations if d.framework != "fastapi"]
            declarations = self._resolve_fastapi_includes(fastapi) + rest

        # 去重：同一 (method, path, handler, owner, route_file) 只保留一条
        unique: dict[tuple[str, str, str, str, str], RouteDeclaration] = {}
        for decl in declarations:
            key = (
                decl.method, decl.path, decl.handler,
                decl.handler_owner, decl.route_file or decl.file,
            )
            unique.setdefault(key, decl)

        self.last_report.scanned_files = scanned
        self.last_report.declarations = len(unique)
        self.last_report.frameworks = sorted(frameworks)
        return list(unique.values())

    def _read_tree(self, path: Path) -> ast.Module | None:
        try:
            source = path.read_text(encoding="utf-8")
            return ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return None

    def _collect_metadata(
        self,
        tree: ast.Module,
        module: str,
        rel_file: str,
        meta: _ProjectMetadata,
    ) -> None:
        """收集顶层函数、类视图与字符串字面量设置。"""
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                meta.functions[(module, node.name)] = (rel_file, node.lineno)
            elif isinstance(node, ast.ClassDef):
                info = self._class_info(node, module, rel_file)
                meta.classes[(module, node.name)] = info
                meta.class_by_name.setdefault(node.name, []).append(info)
            self._collect_literal_assignment(node, meta)

    def _class_info(
        self, node: ast.ClassDef, module: str, rel_file: str
    ) -> _ClassInfo:
        info = _ClassInfo(
            name=node.name,
            module=module,
            file=rel_file,
            line=node.lineno,
            bases=[_simple_name(base) for base in node.bases if _simple_name(base)],
        )
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.methods[item.name] = item.lineno
                action = self._drf_action_spec(item)
                if action:
                    info.actions[item.name] = action
            elif isinstance(item, ast.Assign):
                value = item.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            info.attrs[target.id] = value.value
            elif isinstance(item, ast.AnnAssign):
                value = item.value
                if (
                    isinstance(item.target, ast.Name)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    info.attrs[item.target.id] = value.value
        return info

    @staticmethod
    def _drf_action_spec(func: ast.FunctionDef | ast.AsyncFunctionDef) -> dict | None:
        """读取 `@action(detail=..., methods=[...], url_path=...)` 的静态参数。"""
        for decorator in func.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if RoutePrefixRepair._call_name(decorator.func) != "action":
                continue
            detail = False
            for keyword in decorator.keywords:
                if keyword.arg == "detail":
                    detail = (
                        isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    )
            methods: list[str] = []
            for keyword in decorator.keywords:
                if keyword.arg == "methods" and isinstance(
                    keyword.value, (ast.List, ast.Tuple, ast.Set)
                ):
                    methods = [
                        element.value.upper()
                        for element in keyword.value.elts
                        if isinstance(element, ast.Constant)
                        and isinstance(element.value, str)
                    ]
            url_path = ""
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "url_path"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    url_path = keyword.value.value
            return {
                "detail": detail,
                "methods": methods or ["GET"],
                "url_path": url_path,
            }
        return None

    def _collect_literal_assignment(self, node: ast.AST, meta: _ProjectMetadata) -> None:
        """收集 `api_prefix = "/api"` 这类字符串常量。

        只按名字取值，且仅当全项目该名字只有一个取值时才用于解析；
        同名不同值的设置视为歧义，宁可让前缀保持未知。
        """
        pairs: list[tuple[str, str]] = []
        if isinstance(node, ast.Assign):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        pairs.append((target.id, value.value))
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            if (
                isinstance(node.target, ast.Name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                pairs.append((node.target.id, value.value))
        for name, value in pairs:
            meta.literals.setdefault(name, set()).add(value)
        # 类体内的设置项（如 class Settings: api_prefix = "/api"）
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                self._collect_literal_assignment(item, meta)

    # ------------------------------------------------------------- AST 工具

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        """取调用的末段名：`include(...)` → include，`urls.include(...)` → include。

        `node` 既可能是调用本身，也可能是调用的 func，两种都接受——
        调用方常常直接传 `call.func`。
        """
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    @staticmethod
    def _dotted_name(node: ast.AST) -> str:
        """拼点号全名：`views.order_list` → "views.order_list"。

        只认 Name/Attribute 链；其它形式（下标、调用返回值）返回空串，不猜。
        """
        parts: list[str] = []
        current: ast.AST | None = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
        return ""

    @staticmethod
    def _kwarg(call: ast.Call, key: str) -> ast.AST | None:
        for keyword in call.keywords:
            if keyword.arg == key:
                return keyword.value
        return None

    def _kwarg_str(self, call: ast.Call, key: str) -> str | None:
        """取字符串关键字参数。未出现返回 None（区别于显式传空串）。"""
        value = self._kwarg(call, key)
        if value is None:
            return None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None

    def _kwarg_list(self, call: ast.Call, key: str) -> list[str]:
        """取字符串列表关键字参数，如 `methods=["GET", "POST"]`。"""
        value = self._kwarg(call, key)
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return []
        return [
            element.value
            for element in value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]

    @staticmethod
    def _first_str_arg(call: ast.Call) -> str | None:
        if not call.args:
            return None
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None

    # ------------------------------------------------------------ Django 部分

    @staticmethod
    def _looks_like_django(tree: ast.Module) -> bool:
        """模块内是否出现 `urlpatterns = [...]` 赋值。"""
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "urlpatterns":
                        return True
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "urlpatterns":
                    return True
        return False

    def _scan_django_module(
        self,
        tree: ast.Module,
        rel_file: str,
        module: str,
        meta: _ProjectMetadata,
    ) -> list[RouteDeclaration]:
        """抽取单个 urls.py 里的 path()/re_path()/url() 声明。

        `include()` 调用在这里**先不展开**——它的前缀取决于谁 include 了本模块，
        需要跨文件求解，交给 `_resolve_django_includes`。这里用 `__include__`
        标记记录 include 边，最后统一做拓扑展开。

        `View.as_view()` 与 DRF `router.register` 在声明处就地展开：
        静态可知的 HTTP method / CRUD 动作都标 `inferred`，并带上
        `handler_owner`，供 `repair` 与图谱的类级路由对齐。
        """
        imports = self._module_imports(tree, rel_file)
        router_ops = self._drf_router_operations(
            tree, rel_file, module, meta, imports
        )
        found: list[RouteDeclaration] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = self._call_name(node.func)
            if func_name not in _DJANGO_ROUTE_FUNCS:
                continue
            if len(node.args) < 2:
                continue

            route_arg = node.args[0]
            if not (
                isinstance(route_arg, ast.Constant)
                and isinstance(route_arg.value, str)
            ):
                continue
            local = normalize_django_path(route_arg.value)

            target = node.args[1]
            if self._call_name(target) == "include":
                router = self._router_include_var(target)
                if router:
                    # router.urls 没有独立模块：注册项直接并回当前 URLconf，
                    # 这样外层 include 前缀会在统一展开时照常累加。
                    for op in router_ops.get(router, []):
                        merged = _join(local, op.raw_path)
                        found.append(
                            RouteDeclaration(
                                framework="django",
                                method=op.method,
                                path=merged,
                                handler=op.handler,
                                file=op.file,
                                line=op.line,
                                raw_path=merged,
                                route_file=rel_file,
                                handler_owner=op.handler_owner,
                                inferred=op.inferred,
                                note=op.note,
                            )
                        )
                    continue
                target_module = self._include_target(target)
                if target_module:
                    # include 边：本模块的 route 前缀 + 目标模块
                    found.append(
                        RouteDeclaration(
                            framework="django",
                            method="",
                            path=local,
                            handler=f"__include__:{target_module}",
                            file=rel_file,
                            line=node.lineno,
                            raw_path=local,
                            route_file=rel_file,
                        )
                    )
                continue

            if (
                isinstance(target, ast.Call)
                and self._call_name(target.func) == "as_view"
            ):
                found.extend(
                    self._as_view_declarations(
                        target, local, rel_file, module, meta, imports
                    )
                )
                continue

            # `path("admin/", admin.site.urls)` 不是普通视图，静态展开不了；
            # 把它当 handler="urls" 会凭空造出一条假接口。
            if isinstance(target, ast.Attribute) and target.attr == "urls":
                continue

            handler = self._dotted_name(target)
            if not handler:
                continue
            handler_name = handler.rsplit(".", 1)[-1]
            handler_file, handler_line = self._handler_location(
                target, rel_file, module, imports, meta
            )
            found.append(
                RouteDeclaration(
                    framework="django",
                    method="",
                    path=local,
                    handler=handler_name,
                    file=handler_file,
                    line=handler_line or node.lineno,
                    raw_path=local,
                    route_file=rel_file,
                )
            )
        return found

    def _module_imports(
        self, tree: ast.Module, rel_file: str
    ) -> dict[str, tuple[str, str]]:
        """局部名 → (模块名, 原名)；原名空串代表导入的是子模块。"""
        module = _module_name_of_file(rel_file)
        package_parts = module.split(".")[:-1]
        imports: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    drop = node.level - 1
                    base = (
                        package_parts[: len(package_parts) - drop]
                        if drop and drop <= len(package_parts)
                        else list(package_parts)
                    )
                    target = ".".join([*base, target]) if target else ".".join(base)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imports[alias.asname or alias.name] = (target, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    imports[local] = (alias.name, "")
        return imports

    @staticmethod
    def _resolve_import_module(
        dotted: str, module: str, imports: dict[str, tuple[str, str]]
    ) -> str:
        """把 `views` / `shop.views` 解析成绝对模块名；解析不了返回空串。"""
        if not dotted:
            return ""
        first, _, rest = dotted.partition(".")
        imported = imports.get(first)
        if imported:
            target, original = imported
            resolved = f"{target}.{original}" if original else target
            return f"{resolved}.{rest}" if rest else resolved
        return dotted if rest else ""

    def _resolve_class(
        self,
        expr: ast.AST,
        module: str,
        imports: dict[str, tuple[str, str]],
        meta: _ProjectMetadata,
    ) -> _ClassInfo | None:
        name = _simple_name(expr)
        if not name:
            return None
        hint = ""
        if isinstance(expr, ast.Name):
            imported = imports.get(name)
            if imported:
                target, original = imported
                hint = f"{target}.{original}" if original else target
            else:
                hint = module
        elif isinstance(expr, ast.Attribute):
            dotted = self._dotted_name(expr)
            hint = self._resolve_import_module(
                dotted.rpartition(".")[0], module, imports
            )
        return meta.class_info(name, hint)

    def _handler_location(
        self,
        target: ast.AST,
        rel_file: str,
        module: str,
        imports: dict[str, tuple[str, str]],
        meta: _ProjectMetadata,
    ) -> tuple[str, int]:
        """解析函数视图定义所在文件与行号；解析不了退回 URLconf 文件。"""
        if isinstance(target, ast.Name):
            imported = imports.get(target.id)
            if imported:
                target_module, original = imported
                if original:
                    hit = meta.functions.get((target_module, original))
                    if hit:
                        return hit
            hit = meta.functions.get((module, target.id))
            return hit or (rel_file, 0)
        if isinstance(target, ast.Attribute):
            dotted = self._dotted_name(target)
            base, _, name = dotted.rpartition(".")
            target_module = self._resolve_import_module(base, module, imports)
            if target_module:
                hit = meta.functions.get((target_module, name))
                if hit:
                    return hit
        return rel_file, 0

    def _as_view_declarations(
        self,
        call: ast.Call,
        local: str,
        rel_file: str,
        module: str,
        meta: _ProjectMetadata,
        imports: dict[str, tuple[str, str]],
    ) -> list[RouteDeclaration]:
        """把 `View.as_view()` 展开成按 HTTP method 区分的声明。

        方法列表来自类里显式定义的 get/post/...；`as_view({"get": "list"})`
        这种显式映射优先。拿不到任何方法时不生成，交给图谱/人工。
        """
        info = self._resolve_class(call.func.value, module, imports, meta)
        if info is None:
            return []

        mapping: dict[str, str] = {}
        if call.args and isinstance(call.args[0], ast.Dict):
            for key, value in zip(call.args[0].keys, call.args[0].values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    mapping[key.value.lower()] = value.value
        if not mapping:
            # 1) 类里显式定义的 HTTP 方法优先；
            # 2) 否则按 DRF 通用视图基类推断 dispatch 到的 action。
            explicit = {
                name: name
                for name in ("get", "post", "put", "patch", "delete", "head", "options")
                if name in info.methods
            }
            inherited: dict[str, str] = {}
            for base in info.bases:
                for method_upper, action in _DRF_GENERIC_VIEW_ACTIONS.get(base, ()):
                    inherited.setdefault(method_upper.lower(), action)
            inherited.update(explicit)  # 显式方法覆盖基类推断
            mapping = inherited

        out: list[RouteDeclaration] = []
        for method, action in mapping.items():
            method_upper = method.upper()
            if method_upper not in _HTTP_METHODS:
                continue
            out.append(
                RouteDeclaration(
                    framework="django",
                    method=method_upper,
                    path=local,
                    handler=action or method,
                    file=info.file,
                    line=info.methods.get(action or method, info.line),
                    raw_path=local,
                    route_file=rel_file,
                    handler_owner=info.name,
                    inferred=True,
                    note="as_view_method_inferred",
                )
            )
        return out

    def _drf_router_operations(
        self,
        tree: ast.Module,
        rel_file: str,
        module: str,
        meta: _ProjectMetadata,
        imports: dict[str, tuple[str, str]],
    ) -> dict[str, list[RouteDeclaration]]:
        """展开 `router.register(prefix, ViewSet)` 的 CRUD 路由。

        DRF 的 `router.urls` 是运行时生成的；静态展开只能覆盖 ViewSet/Mixin
        约定内的动作，且一律标 `inferred`。无法确定类定义或动作集合时宁可不生成。
        """
        routers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                call = node.value
                if (
                    isinstance(call, ast.Call)
                    and self._call_name(call.func)
                    in {"DefaultRouter", "SimpleRouter"}
                ):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            routers.add(target.id)
            elif isinstance(node, ast.AnnAssign):
                call = node.value
                if (
                    isinstance(call, ast.Call)
                    and self._call_name(call.func)
                    in {"DefaultRouter", "SimpleRouter"}
                    and isinstance(node.target, ast.Name)
                ):
                    routers.add(node.target.id)
        if not routers:
            return {}

        operations: dict[str, list[RouteDeclaration]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if self._call_name(node.func) != "register":
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or not isinstance(
                func.value, ast.Name
            ):
                continue
            router = func.value.id
            if router not in routers or len(node.args) < 2:
                continue
            prefix_arg, class_expr = node.args[0], node.args[1]
            if not (
                isinstance(prefix_arg, ast.Constant)
                and isinstance(prefix_arg.value, str)
            ):
                continue
            info = self._resolve_class(class_expr, module, imports, meta)
            if info is None:
                continue
            base = normalize_django_path(prefix_arg.value)
            lookup = info.attrs.get("lookup_field") or "pk"
            for path, method, handler in self._drf_operations(info, base, lookup):
                operations.setdefault(router, []).append(
                    RouteDeclaration(
                        framework="django",
                        method=method,
                        path=path,
                        handler=handler,
                        file=info.file,
                        line=info.methods.get(handler, info.line),
                        raw_path=path,
                        route_file=rel_file,
                        handler_owner=info.name,
                        inferred=True,
                        note="drf_router_expanded",
                    )
                )
        return operations

    def _drf_operations(
        self, info: _ClassInfo, base: str, lookup: str
    ) -> list[tuple[str, str, str]]:
        """返回 [(path, method, handler)]，按 DRF 约定展开。"""
        actions: set[str] = set(info.methods)
        for name in info.bases:
            actions.update(_DRF_BASE_ACTIONS.get(name, ()))
            actions.update(_DRF_MIXIN_ACTIONS.get(name, ()))

        out: list[tuple[str, str, str]] = []
        for action, method in _DRF_ACTION_METHODS.items():
            if action not in actions:
                continue
            detail = action in {"retrieve", "update", "partial_update", "destroy"}
            path = (
                _join(base, "{" + lookup + "}/")
                if detail
                else _join(base, "/")
            )
            out.append((path, method, action))

        for action, spec in info.actions.items():
            url_path = spec.get("url_path") or action
            detail = bool(spec.get("detail"))
            segments = ["{" + lookup + "}"] if detail else []
            path = _join(base, *segments, url_path + "/")
            for method in spec.get("methods") or ["GET"]:
                if method.upper() in _HTTP_METHODS:
                    out.append((path, method.upper(), action))
        return out

    @staticmethod
    def _router_include_var(call: ast.Call) -> str:
        """`include(router.urls)` → router；其它形式返回空串。"""
        if not call.args:
            return ""
        arg = call.args[0]
        if isinstance(arg, ast.Tuple) and arg.elts:
            arg = arg.elts[0]
        if (
            isinstance(arg, ast.Attribute)
            and arg.attr == "urls"
            and isinstance(arg.value, ast.Name)
        ):
            return arg.value.id
        return ""

    @staticmethod
    def _include_target(call: ast.Call) -> str | None:
        """从 `include("shop.urls")` 取模块名；`include((patterns, "ns"))` 取不到则不猜。"""
        if not call.args:
            return None
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Tuple) and arg.elts:
            first = arg.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
        return None

    def _resolve_django_includes(self, decls: list[RouteDeclaration]) -> list[RouteDeclaration]:
        """把 include 边展开成完整路径。

        算法：以「URLconf 模块名 → 该模块内的声明」建图，从没有任何模块
        include 它的模块（根 urlconf）出发，沿 include 边累加前缀做 DFS。

        `file` 是 handler 所在文件，`route_file` 才是 URLconf；分组必须用后者，
        否则 `views.py` 会被误当成 URLconf 模块。

        环保护：include 自引用会形成环，用 visiting 集合截断。
        """
        file_to_module: dict[str, str] = {}
        for decl in decls:
            route_file = decl.route_file or decl.file
            file_to_module[route_file] = _module_name_of_file(route_file)

        module_decls: dict[str, list[RouteDeclaration]] = {}
        for decl in decls:
            module = file_to_module.get(decl.route_file or decl.file, decl.file)
            module_decls.setdefault(module, []).append(decl)

        included_modules = {
            d.handler.split(":", 1)[1]
            for d in decls
            if d.handler.startswith("__include__:")
        }
        # 根候选：被扫描到、但从未被别人 include 的模块
        roots = [m for m in module_decls if m not in included_modules]
        # 没有根（全被 include，说明根 urlconf 未被索引）时，退化为全部模块各自从头算
        if not roots:
            roots = list(module_decls)

        resolved: list[RouteDeclaration] = []
        seen: set[tuple[str, str, str, str]] = set()

        def walk(module: str, prefix: str, visiting: set[str]) -> None:
            if module in visiting or module not in module_decls:
                return
            visiting = visiting | {module}
            for decl in module_decls[module]:
                if decl.handler.startswith("__include__:"):
                    target = decl.handler.split(":", 1)[1]
                    walk(target, _join(prefix, decl.raw_path), visiting)
                    continue
                full = _join(prefix, decl.raw_path)
                key = (decl.method, full, decl.handler, decl.handler_owner)
                if key in seen:
                    continue
                seen.add(key)
                resolved.append(
                    RouteDeclaration(
                        framework="django",
                        method=decl.method,
                        path=full,
                        handler=decl.handler,
                        file=decl.file,
                        line=decl.line,
                        raw_path=decl.raw_path,
                        route_file=decl.route_file or decl.file,
                        handler_owner=decl.handler_owner,
                        inferred=decl.inferred,
                        note=decl.note,
                    )
                )

        for root in roots:
            walk(root, "", set())
        return resolved

    # ------------------------------------------------------------- Flask 部分

    def _scan_flask_module(self, tree: ast.Module, rel_file: str) -> list[RouteDeclaration]:
        """抽取 Flask 的 `@app.route` / `@app.get` / `@bp.route` 声明。

        前缀解析顺序（后者覆盖前者，与 Flask 运行时一致）：
          1. `Blueprint(..., url_prefix=...)` 的定义处取默认前缀
          2. 模块内 `app.register_blueprint(bp, url_prefix=...)` 覆盖
        """
        # 变量名 → 前缀（app 变量本身前缀为空）
        prefixes: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if self._call_name(call.func) == "Blueprint":
                    prefix = self._kwarg_str(call, "url_prefix") or ""
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            prefixes[target.id] = prefix
                elif self._call_name(call.func) in {"Flask", "Quart"}:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            prefixes.setdefault(target.id, "")
                # 注意：FastAPI/APIRouter 不登记为 Flask 前缀。
                # 否则 `@router.get` 会被 Flask 扫描器重复识别成一条无前缀路由。

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if self._call_name(node.func) != "register_blueprint" or not node.args:
                continue
            bp = node.args[0]
            if not isinstance(bp, ast.Name):
                continue
            override = self._kwarg_str(node, "url_prefix")
            if override is not None:
                prefixes[bp.id] = override

        found: list[RouteDeclaration] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            methods, route_path = self._flask_decorators(node, prefixes)
            if route_path is None:
                continue
            prefix, local = route_path
            full = _join(prefix, local)
            for method in methods:
                found.append(
                    RouteDeclaration(
                        framework="flask",
                        method=method,
                        path=full,
                        handler=node.name,
                        file=rel_file,
                        line=node.lineno,
                        raw_path=local,
                        route_file=rel_file,
                    )
                )
        return found

    def _flask_decorators(
        self, func: ast.FunctionDef | ast.AsyncFunctionDef, prefixes: dict[str, str]
    ) -> tuple[list[str], tuple[str, str] | None]:
        """解析函数上的路由装饰器，返回 (methods, (prefix, local_path))。"""
        for decorator in func.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            attr = decorator.func
            if not isinstance(attr, ast.Attribute):
                continue
            owner = attr.value
            if not isinstance(owner, ast.Name):
                continue
            local = self._first_str_arg(decorator)
            if local is None:
                continue

            # 快捷装饰器（@app.get 等）只认本模块显式声明的 Flask/Quart 实例；
            # FastAPI 的 @router.get 形状相同，必须在这里排除，不能默认空前缀硬认。
            if attr.attr.upper() in _HTTP_METHODS and owner.id not in prefixes:
                continue
            prefix = prefixes.get(owner.id, "")

            if attr.attr == "route":
                methods = self._kwarg_list(decorator, "methods") or ["GET"]
                return [m.upper() for m in methods], (prefix, local)
            if attr.attr.upper() in _HTTP_METHODS:
                return [attr.attr.upper()], (prefix, local)
        return [], None

    # ----------------------------------------------------------- FastAPI 部分

    _FASTAPI_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

    def _fastapi_imports(self, tree: ast.Module, rel_file: str) -> dict[str, tuple[str, str]]:
        """局部名 → (模块名, 原名)，含相对导入与 `import x as y`。"""
        return self._module_imports(tree, rel_file)

    @staticmethod
    def _prefix_expression(
        expr: ast.AST | None, meta: _ProjectMetadata
    ) -> tuple[str, bool, bool]:
        """解析 prefix 表达式 → (值, 是否推导, 是否确定)。

        `APIRouter(prefix="/api")` → ("/api", False, True)
        字面量设置 `api_prefix = "/api"` → ("/api", True, True)
        `prefix=settings.api_prefix` 但项目里找不到唯一字面量 → ("", True, False)
        """
        if expr is None:
            return "", False, True
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value, False, True
        name = ""
        if isinstance(expr, ast.Name):
            name = expr.id
        elif isinstance(expr, ast.Attribute):
            name = expr.attr
        if name:
            literal = meta.literal(name)
            if literal is not None:
                return literal, True, True
        return "", True, False

    def _scan_fastapi_module(
        self,
        tree: ast.Module,
        rel_file: str,
        module: str,
        meta: _ProjectMetadata,
    ) -> list[RouteDeclaration]:
        """抽取 FastAPI 的 `@app.get/post/...`、`@router.get/...` 与 include_router。

        前缀解析：
          1. `APIRouter(prefix="/api/v1")` 绑定到变量；非字面量（如
             `settings.api_prefix`）尝试从全项目唯一字面量设置还原；
          2. `app.include_router(x.router, prefix=...)` 支持 `module.router`
             与导入别名；跨文件/同文件叠加；
          3. 最终路径 = include 前缀 + router 前缀 + 装饰器路径。
        任何推导出的前缀都会把路由标成 `inferred`，不冒充显式声明。
        """
        prefixes: dict[str, str] = {}
        prefix_inferred: dict[str, bool] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            if self._call_name(call.func) not in {"FastAPI", "APIRouter", "Quart"}:
                continue
            value, inferred, known = self._prefix_expression(
                self._kwarg(call, "prefix"), meta
            )
            if not known:
                self.last_report.unresolved_prefixes += 1
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    prefixes[target.id] = value
                    prefix_inferred[target.id] = inferred

        imports = self._fastapi_imports(tree, rel_file)
        found: list[RouteDeclaration] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for method, local, owner in self._fastapi_decorators(node, prefixes):
                inferred = prefix_inferred.get(owner, False)
                found.append(
                    RouteDeclaration(
                        framework="fastapi",
                        method=method,
                        path=_join(prefixes.get(owner, ""), local),
                        handler=node.name,
                        file=rel_file,
                        line=node.lineno,
                        raw_path=local,
                        owner=owner,
                        route_file=rel_file,
                        inferred=inferred,
                        note="router_prefix_from_literal" if inferred else "",
                    )
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if self._call_name(node.func) != "include_router" or not node.args:
                continue
            resolved = self._fastapi_include_target(
                node.args[0], module, imports, prefixes, meta
            )
            if resolved is None:
                # 目标是动态构造/别名链，静态不可判 → 不猜，留在报告里
                continue
            target_module, target_owner = resolved
            value, inferred, known = self._prefix_expression(
                self._kwarg(node, "prefix"), meta
            )
            if not known:
                self.last_report.unresolved_prefixes += 1
            found.append(
                RouteDeclaration(
                    framework="fastapi",
                    method="",
                    path=value,
                    handler=f"__include_fastapi__:{target_module}:{target_owner}",
                    file=rel_file,
                    line=node.lineno,
                    raw_path=value,
                    owner=target_owner,
                    route_file=rel_file,
                    inferred=inferred,
                )
            )
        return found

    def _fastapi_include_target(
        self,
        target: ast.AST,
        module: str,
        imports: dict[str, tuple[str, str]],
        prefixes: dict[str, str],
        meta: _ProjectMetadata,
    ) -> tuple[str, str] | None:
        """解析 `include_router` 目标 → (目标模块, 目标 router 变量)。

        必须区分「导入的是子模块」和「导入的是模块内变量」：
        - `from app.api.routes import authentication` → 子模块，变量名 router；
        - `from app.api.routes.api import router as api_router` → 变量，原名 router；
        - `import app.api.routes.api as api` + `api.router` → 模块属性。
        """
        if isinstance(target, ast.Name):
            imported = imports.get(target.id)
            if imported:
                target_module, original = imported
                if original and f"{target_module}.{original}" in meta.modules:
                    return f"{target_module}.{original}", "router"
                if original:
                    return target_module, original
                return target_module, "router"
            if target.id in prefixes:
                return module, target.id
            return None

        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            base, attr = target.value.id, target.attr
            imported = imports.get(base)
            if not imported:
                return None
            target_module, original = imported
            if original:
                candidate = f"{target_module}.{original}"
                if candidate in meta.modules:
                    return candidate, attr
                # 导入的是模块内变量（常见 router）；`api.router` 还原到变量本身
                if attr == "router":
                    return target_module, original
                return None
            return target_module, attr
        return None

    def _fastapi_decorators(
        self, func: ast.FunctionDef | ast.AsyncFunctionDef, prefixes: dict[str, str]
    ) -> list[tuple[str, str, str]]:
        """返回 [(method, local_path, owner_var)]。"""
        out: list[tuple[str, str, str]] = []
        for decorator in func.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            attr = decorator.func
            if not isinstance(attr, ast.Attribute) or not isinstance(attr.value, ast.Name):
                continue
            owner = attr.value.id
            if owner not in prefixes:
                continue
            local = self._first_str_arg(decorator)
            if local is None:
                continue
            name = attr.attr.lower()
            if name == "api_route":
                methods = self._kwarg_list(decorator, "methods") or ["GET"]
                for method in methods:
                    if method.upper() in self._FASTAPI_METHODS:
                        out.append((method.upper(), local, owner))
            elif name.upper() in self._FASTAPI_METHODS:
                out.append((name.upper(), local, owner))
        return out

    @staticmethod
    def _decl_key(decl: RouteDeclaration) -> tuple[str, str, str, str, int]:
        return (
            decl.method, decl.path, decl.handler,
            decl.handler_owner, decl.line,
        )

    def _resolve_fastapi_includes(self, decls: list[RouteDeclaration]) -> list[RouteDeclaration]:
        """展开 include_router：从根模块出发，逐层累加前缀。"""
        file_to_module = {
            (d.route_file or d.file): _module_name_of_file(d.route_file or d.file)
            for d in decls
        }
        module_decls: dict[str, list[RouteDeclaration]] = {}
        for decl in decls:
            module_decls.setdefault(
                file_to_module[decl.route_file or decl.file], []
            ).append(decl)

        included: set[str] = set()
        self_included: set[tuple[str, str]] = set()
        for module, items in module_decls.items():
            for decl in items:
                if not decl.handler.startswith("__include_fastapi__:"):
                    continue
                _, target_module, target_owner = decl.handler.split(":", 2)
                if target_module == module:
                    self_included.add((module, target_owner))
                else:
                    included.add(target_module)

        roots = [m for m in module_decls if m not in included] or list(module_decls)
        resolved: list[RouteDeclaration] = []
        seen: set[tuple[str, str, str, str]] = set()
        emitted_normals: set[tuple[str, str, str, str, int]] = set()

        def emit(decl: RouteDeclaration, prefix: str, inferred: bool) -> None:
            path = _join(prefix, decl.path)
            key = (decl.method, path, decl.handler, decl.handler_owner)
            if key in seen:
                return
            seen.add(key)
            if not decl.handler.startswith("__include_fastapi__:"):
                emitted_normals.add(self._decl_key(decl))
            note = decl.note
            if inferred and not decl.inferred and not note:
                # 前缀推断来自 include 侧（如 settings.api_prefix），如实标注来源
                note = "include_prefix_inferred"
            resolved.append(
                RouteDeclaration(
                    framework="fastapi",
                    method=decl.method,
                    path=path,
                    handler=decl.handler,
                    file=decl.file,
                    line=decl.line,
                    raw_path=decl.raw_path,
                    owner=decl.owner,
                    route_file=decl.route_file or decl.file,
                    handler_owner=decl.handler_owner,
                    inferred=inferred or decl.inferred,
                    note=note,
                )
            )

        def walk(
            module: str,
            prefix: str,
            owner_filter: str | None,
            visiting: set[str],
            inherited_inferred: bool,
        ) -> None:
            if module in visiting or module not in module_decls:
                return
            visiting = visiting | {module}
            for decl in module_decls[module]:
                if decl.handler.startswith("__include_fastapi__:"):
                    _, target_module, target_owner = decl.handler.split(":", 2)
                    new_prefix = _join(prefix, decl.raw_path)
                    new_inferred = inherited_inferred or decl.inferred
                    if target_module == module:
                        # 同模块 include：只把该 owner 的路由按 include 前缀再输出一份
                        for inner in module_decls[module]:
                            if inner.handler.startswith("__include_fastapi__:"):
                                continue
                            if inner.owner == target_owner:
                                emit(inner, new_prefix, new_inferred)
                    else:
                        walk(
                            target_module,
                            new_prefix,
                            target_owner or None,
                            visiting,
                            new_inferred,
                        )
                    continue
                if owner_filter is not None and decl.owner != owner_filter:
                    continue
                if owner_filter is None and (module, decl.owner) in self_included:
                    # 该 router 通过同模块 include 注册，基路径版本不应再输出
                    continue
                emit(decl, prefix, inherited_inferred)

        for root in roots:
            walk(root, "", None, set(), False)
        # 根候选遗漏的模块（被 include 但目标未被扫描到）不静默丢弃；
        # 但已经通过某个 owner 输出过的路由不能按空前缀再输出一份。
        for module, items in module_decls.items():
            for decl in items:
                if decl.handler.startswith("__include_fastapi__:"):
                    continue
                if self._decl_key(decl) in emitted_normals:
                    continue
                emit(decl, "", decl.inferred)
        return resolved

    # --------------------------------------------------------------- 修复主流程

    def repair(
        self,
        graph_routes: list[RouteRef],
        declarations: list[RouteDeclaration] | None = None,
    ) -> list[RouteRef]:
        """用重建结果修复图谱路由。

        输出包含三类路由（全部返回，靠 `confidence` 区分）：
        - 图谱与重建一致/可后缀对齐 → `verified`，路径用重建值
        - 仅重建有 → `verified`，`source=framework_plugin`
        - 冲突 → `unmapped`，两路证据都保留在最后一个 RouteRef
        """
        decls = declarations if declarations is not None else self.scan()
        report = self.last_report

        # 索引：handler 名 → 声明（多对多，Django 允许同名 handler 挂不同前缀）
        by_handler: dict[str, list[RouteDeclaration]] = {}
        for decl in decls:
            by_handler.setdefault(decl.handler, []).append(decl)

        repaired: list[RouteRef] = []
        used: set[tuple[str, str, str, str]] = set()

        for route in graph_routes:
            candidates = by_handler.get(route.handler, [])
            if not candidates:
                if self._covered_by_owner(route, decls):
                    # Django/DRF 的图谱路由是类级、无 method；插件已按类展开成
                    # 具体 action。保留图谱那条只会制造 "route_method_missing"，
                    # 且与展开后的路由重复。
                    report.superseded_graph_routes += 1
                    continue
                # 图谱单方证据，最多算 inferred——没有第二来源可交叉校验
                report.graph_only += 1
                repaired.append(
                    RouteRef(
                        method=route.method,
                        path=_ensure_leading_slash(route.path),
                        handler=route.handler,
                        file=route.file,
                        line=route.line,
                        handler_id=route.handler_id,
                        route_file=route.route_file,
                        route_line=route.route_line,
                        source="graph",
                        confidence=INFERRED,
                        note="no_source_evidence",
                    )
                )
                continue

            match = self._best_match(route, candidates)
            if match is None:
                # 有同名声明但路径对不上 → 冲突，不猜
                report.conflicts += 1
                candidate_paths = ", ".join(sorted({c.path for c in candidates})[:3])
                repaired.append(
                    RouteRef(
                        method=route.method or candidates[0].method,
                        path=_ensure_leading_slash(route.path),
                        handler=route.handler,
                        file=route.file,
                        line=route.line,
                        handler_id=route.handler_id,
                        route_file=route.route_file,
                        route_line=route.route_line,
                        source="graph",
                        confidence=UNMAPPED,
                        conflict_with=candidate_paths,
                    )
                )
                continue

            used.add(
                (match.method, match.path, match.handler, match.handler_owner)
            )
            report.matched += 1
            graph_path = _ensure_leading_slash(route.path)
            graph_tail = _tail(graph_path)
            if graph_tail != _tail(match.path):
                report.prefix_repaired += 1
            # 图谱路径为空（只有 `/`）时，对齐只靠 handler 名，路径没有
            # 参与交叉校验；不能声称 verified。
            path_unverified = not graph_tail and bool(_tail(match.path))
            note = match.note
            if path_unverified and not note:
                note = "graph_path_missing"
            repaired.append(
                RouteRef(
                    # method 以源码为准（Django 源码里也没有则为空，保持诚实）
                    method=match.method or route.method,
                    path=match.path,
                    handler=match.handler or route.handler,
                    handler_owner=match.handler_owner,
                    file=route.file or match.file,
                    line=route.line or match.line,
                    handler_id=route.handler_id,
                    route_file=route.route_file or match.route_file or match.file,
                    route_line=route.route_line or match.line,
                    source="graph_repaired",
                    confidence=(
                        INFERRED
                        if match.inferred or path_unverified
                        else VERIFIED
                    ),
                    note=note,
                )
            )

        # 重建有、图谱没有的：图谱漏识别，但源码证据充分 → 补进来。
        # 由框架约定推导出的动作（as_view / DRF）不能冒充 verified。
        for decl in decls:
            key = (
                decl.method, decl.path, decl.handler, decl.handler_owner
            )
            if key in used:
                continue
            report.plugin_only += 1
            repaired.append(
                RouteRef(
                    method=decl.method,
                    path=decl.path,
                    handler=decl.handler,
                    handler_owner=decl.handler_owner,
                    file=decl.file,
                    line=decl.line,
                    route_file=decl.route_file or decl.file,
                    route_line=decl.line,
                    source="framework_plugin",
                    confidence=INFERRED if decl.inferred else VERIFIED,
                    note=decl.note or "graph_missed",
                )
            )

        return repaired

    def _covered_by_owner(
        self, route: RouteRef, decls: list[RouteDeclaration]
    ) -> bool:
        """图谱的类级路由是否已被同一类的展开声明覆盖。

        只按 handler 名匹配会漏掉 `ArticleViewSet` → `list/create/...` 的展开；
        这里按类名 + 路径后缀确认，避免把无关类误判为已覆盖。
        """
        owner = route.handler
        if not owner:
            return False
        graph_path = _tail(_ensure_leading_slash(route.path))
        for decl in decls:
            if decl.handler_owner != owner:
                continue
            decl_path = _tail(decl.path)
            if (
                graph_path == decl_path
                or _is_path_suffix(graph_path, decl_path)
                or _is_path_suffix(decl_path, graph_path)
            ):
                return True
        return False

    def _best_match(
        self, route: RouteRef, candidates: list[RouteDeclaration]
    ) -> RouteDeclaration | None:
        """在同一 handler 的多个声明里，按路径后缀一致性挑唯一匹配。

        后缀规则：图谱的裸路径必须等于重建路径，或按**完整路径段**是其后缀，
        避免 `/orders` 误配 `/api/v1/backorders`。
        """
        graph_path = _tail(_ensure_leading_slash(route.path))

        exact = [c for c in candidates if _tail(c.path) == graph_path]
        if exact:
            # 多条完全一致的声明（重复注册）取第一条，路径正确性不受影响
            return exact[0]

        # FastAPI 在部分仓库里会把路由前缀整个丢掉，图谱只剩 `/`。
        # 此时路径不提供任何消歧信息，唯一可用证据是 handler 名；
        # 只有该 handler 在源码里唯一对应一个路径时才对齐，多路径仍然不猜。
        if not graph_path:
            distinct_paths = {_tail(c.path) for c in candidates}
            if len(distinct_paths) == 1:
                return candidates[0]

        tail_matches = [c for c in candidates if _is_path_suffix(graph_path, _tail(c.path))]
        if len(tail_matches) == 1:
            return tail_matches[0]
        # 多个候选中无法唯一定位 → 不猜
        return None


def _ensure_leading_slash(path: str) -> str:
    if not path:
        return "/"
    return path if path.startswith("/") else "/" + path


def _is_path_suffix(short: str, full: str) -> bool:
    """`short` 是否按完整路径段是 `full` 的后缀。

    `/orders`      vs `/api/v1/orders`  → True
    `/orders`      vs `/api/v1/backorders` → False
    `/v1/orders`   vs `/api/v1/orders`  → True
    """
    if not short or not full:
        return False
    if short == full:
        return True
    return full.endswith("/" + short.lstrip("/"))
