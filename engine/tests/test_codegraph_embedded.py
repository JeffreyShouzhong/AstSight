"""CodeGraph 代码嵌入接入验证。

三组断言：

  A. 路由前缀重建（离线，不依赖 Node）——这是本轮最重要的修正。
     实测 CodeGraph 1.6.2 对 Flask Blueprint 的 url_prefix、Django include()
     的前缀都不拼接，且 Django 完全没有 HTTP method。若框架直接采用图谱路径，
     测试会打到缺前缀的 URL 得到 404，把失败归因到无关模块——典型的"测试假红"。

  B. 证据等级不可被绕过。冲突标 unmapped、图谱单方标 inferred、
     源码独立印证才给 verified。宁可漏、不可错。

  C. 嵌入式客户端能起来并打通一个真实索引（需 Node>=22.5 与已安装的
     @colbymchenry/codegraph；环境不具备时跳过，不伪装通过）。

跑法：cd engine && PYTHONPATH=. python tests/test_codegraph_embedded.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codegraph import (  # noqa: E402
    INFERRED,
    UNMAPPED,
    VERIFIED,
    RoutePrefixRepair,
    RouteRef,
)

_FAILS: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{'  <- ' + detail if detail else ''}")
        _FAILS.append(label)


def write(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


# --------------------------------------------------------------------- A
def test_flask_blueprint_prefix() -> None:
    print("\n=== A1. Flask Blueprint 前缀必须被补回 ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(
            root,
            "app/orders.py",
            """
            from flask import Blueprint

            bp = Blueprint("orders", __name__, url_prefix="/api/v1")

            @bp.route("/orders/<int:order_id>", methods=["PUT"])
            def update_order(order_id):
                return {}

            @bp.get("/orders")
            def list_orders():
                return {}
            """,
        )

        # 图谱给出的正是实测结果：前缀丢失的裸路径
        graph = [
            RouteRef(method="PUT", path="/orders/<int:order_id>", handler="update_order",
                     file="app/orders.py", line=6),
            RouteRef(method="GET", path="/orders", handler="list_orders",
                     file="app/orders.py", line=10),
        ]
        repair = RoutePrefixRepair(root)
        routes = {r.handler: r for r in repair.repair(graph)}

        check("update_order 路径补上 /api/v1",
              routes["update_order"].path == "/api/v1/orders/<int:order_id>",
              routes["update_order"].path)
        check("list_orders 路径补上 /api/v1",
              routes["list_orders"].path == "/api/v1/orders", routes["list_orders"].path)
        check("两条都是 verified",
              all(r.confidence == VERIFIED for r in routes.values()))
        check("来源标为 graph_repaired",
              all(r.source == "graph_repaired" for r in routes.values()))
        check("前缀修复计数为 2", repair.last_report.prefix_repaired == 2,
              str(repair.last_report.prefix_repaired))


# --------------------------------------------------------------------- A
def test_django_include_prefix() -> None:
    print("\n=== A2. Django include() 前缀必须被展开 ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(
            root,
            "config/rooturls.py",
            """
            from django.urls import path, include

            urlpatterns = [
                path("api/v1/", include("shop.urls")),
            ]
            """,
        )
        write(
            root,
            "shop/urls.py",
            """
            from django.urls import path
            from . import views

            urlpatterns = [
                path("orders/", views.order_list, name="order-list"),
                path("orders/<int:order_id>/", views.order_detail, name="order-detail"),
            ]
            """,
        )
        write(
            root,
            "shop/views.py",
            """
            def order_list(request):
                return None

            def order_detail(request, order_id):
                return None
            """,
        )

        # 图谱实测输出：include 前缀丢失、无 method、无前导斜杠
        graph = [
            RouteRef(method="", path="orders/", handler="order_list",
                     file="shop/views.py", line=1),
            RouteRef(method="", path="orders/<int:order_id>/", handler="order_detail",
                     file="shop/views.py", line=4),
        ]
        repair = RoutePrefixRepair(root)
        routes = {r.handler: r for r in repair.repair(graph)}

        check("order_list 路径展开为 /api/v1/orders/",
              routes["order_list"].path == "/api/v1/orders/", routes["order_list"].path)
        check("order_detail 路径展开为 /api/v1/orders/<int:order_id>/",
              routes["order_detail"].path == "/api/v1/orders/<int:order_id>/",
              routes["order_detail"].path)
        check("Django 无 method 时不臆造",
              routes["order_list"].method == "", repr(routes["order_list"].method))
        check("两条都是 verified",
              all(r.confidence == VERIFIED for r in routes.values()))


# --------------------------------------------------------------------- B
def test_conflict_is_unmapped() -> None:
    print("\n=== B1. 两路证据冲突 → unmapped，不猜 ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(
            root,
            "app/web.py",
            """
            from flask import Flask

            app = Flask(__name__)

            @app.get("/health")
            def health():
                return {}
            """,
        )
        # 图谱说这个 handler 挂在 /metrics 下，源码说 /health —— 两路不一致
        graph = [RouteRef(method="GET", path="/metrics", handler="health",
                          file="app/web.py", line=6)]
        repair = RoutePrefixRepair(root)
        routes = repair.repair(graph)

        check("冲突标 unmapped", routes[0].confidence == UNMAPPED, routes[0].confidence)
        check("冲突计数为 1", repair.last_report.conflicts == 1)
        check("保留了源码侧证据供复核", routes[0].conflict_with == "/health",
              routes[0].conflict_with)
        check("路径不被无脑覆盖", routes[0].path == "/metrics", routes[0].path)


# --------------------------------------------------------------------- B
def test_graph_only_is_inferred() -> None:
    print("\n=== B2. 只有图谱证据 → 最多 inferred ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(root, "app/empty.py", "VALUE = 1\n")

        graph = [RouteRef(method="GET", path="/ghost", handler="ghost_handler",
                          file="app/empty.py", line=1)]
        repair = RoutePrefixRepair(root)
        routes = repair.repair(graph)

        check("标 inferred", routes[0].confidence == INFERRED, routes[0].confidence)
        check("注明无源码证据", routes[0].note == "no_source_evidence", routes[0].note)


# --------------------------------------------------------------------- A
def test_plugin_fills_graph_miss() -> None:
    print("\n=== A3. 图谱漏报，源码补齐并标 framework_plugin ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(
            root,
            "app/api.py",
            """
            from flask import Flask

            app = Flask(__name__)

            @app.post("/api/pay")
            def pay():
                return {}

            @app.get("/api/refund")
            def refund():
                return {}
            """,
        )
        repair = RoutePrefixRepair(root)
        routes = repair.repair([])  # 图谱一条都没给

        check("补出 2 条", len(routes) == 2, str(len(routes)))
        check("全部标 framework_plugin",
              all(r.source == "framework_plugin" for r in routes))
        check("全部 verified",
              all(r.confidence == VERIFIED for r in routes))
        check("method 正确",
              {(r.method, r.path) for r in routes} == {("POST", "/api/pay"), ("GET", "/api/refund")},
              str({(r.method, r.path) for r in routes}))


# --------------------------------------------------------------------- B
def test_suffix_match_safety() -> None:
    print("\n=== B3. 后缀匹配按路径段对齐，不误配 ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(
            root,
            "app/shop.py",
            """
            from flask import Blueprint

            bp = Blueprint("shop", __name__, url_prefix="/api/v1")

            @bp.get("/backorders")
            def backorders():
                return {}
            """,
        )
        # 图谱裸路径 /orders 不应匹配 /api/v1/backorders
        graph = [RouteRef(method="GET", path="/orders", handler="backorders",
                          file="app/shop.py", line=6)]
        repair = RoutePrefixRepair(root)
        routes = repair.repair(graph)

        check("不误配为 backorders", routes[0].confidence == UNMAPPED,
              f"{routes[0].confidence} {routes[0].path}")


# --------------------------------------------------------------------- C
def _build_project(root: Path, decorator_style: str) -> None:
    write(root, "requirements.txt", "flask==3.0.3\n")
    write(root, "app/__init__.py", "")
    write(
        root,
        "app/service.py",
        """
        def charge(token):
            return {"ok": True}


        def reserve(sku, qty):
            return True


        def create_order(sku, qty, token):
            reserve(sku, qty)
            charge(token)
            return {"id": "o-1"}
        """,
    )
    # 两种装饰器写法语义等价，但 CodeGraph 的 Flask resolver 只认前者
    decorator = (
        '@app.route("/api/orders", methods=["POST"])'
        if decorator_style == "route"
        else '@app.post("/api/orders")'
    )
    write(
        root,
        "app/api.py",
        f"""
        from flask import Flask
        from .service import create_order

        app = Flask(__name__)

        {decorator}
        def create_order_route():
            return create_order("SKU-1", 1, "tok")
        """,
    )


def _module_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("ASTSIGHT_CODEGRAPH_MODULE_ROOT")
    if env:
        candidates.append(Path(env))
    candidates.append(Path("D:/testhub_platform/codegraph_lab/node_modules"))
    candidates.append(Path(__file__).resolve().parents[1] / "node_modules")
    return candidates


def _find_node() -> Path | None:
    env = os.environ.get("ASTSIGHT_NODE")
    if env and Path(env).is_file():
        return Path(env)
    managed = sorted(
        Path.home().glob(".workbuddy/binaries/node/versions/*/node.exe"),
        reverse=True,
    )
    for candidate in managed:
        if candidate.is_file():
            return candidate
    found = shutil.which("node") or shutil.which("node.exe")
    return Path(found) if found else None


def _require_env() -> tuple[Path, Path] | None:
    module_root = next(
        (
            candidate for candidate in _module_root_candidates()
            # 只判断目录存在会把「空壳目录」当成已安装，随后 worker 启动失败；
            # package.json 是可 require 的最小证据。
            if (
                candidate / "@colbymchenry/codegraph" / "package.json"
            ).is_file()
        ),
        None,
    )
    node_bin = _find_node()
    if module_root is None:
        print("  SKIP  CodeGraph 未安装（可用 ASTSIGHT_CODEGRAPH_MODULE_ROOT 指定）")
        return None
    if node_bin is None:
        print("  SKIP  未找到 Node（库模式要求 >= 22.5）")
        return None
    os.environ["ASTSIGHT_CODEGRAPH_MODULE_ROOT"] = str(module_root)
    return module_root, node_bin


def test_embedded_client_shortcut_decorator() -> None:
    """实测缺口：Flask 的 @app.get/@app.post 不被 CodeGraph 识别。

    这是 Flask 2.0+ 官方主推写法，纯 Flask 项目若全用 shortcut，
    图谱侧一条路由都不会产出。此时必须由框架插件兜底——这证明
    路由插件是**必需品**，不是可选增强。
    """
    print("\n=== C1. Flask shortcut 装饰器：图谱漏识别，插件兜底 ===")
    env = _require_env()
    if env is None:
        return
    module_root, node_bin = env

    from codegraph import EmbeddedCodeGraphClient  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_project(root, "shortcut")

        client = EmbeddedCodeGraphClient(root, module_root=module_root, node=str(node_bin))
        try:
            client.ensure_index(full=True)
            stats = client.stats()
            route_nodes = (stats.get("nodesByKind") or {}).get("route", 0)
            check("@app.post 未产出 route 节点（已知缺口）", route_nodes == 0, str(route_nodes))

            routes = client.find_routes()
            by_handler = {r.handler: r for r in routes}
            check("插件补出 create_order_route", "create_order_route" in by_handler,
                  str(list(by_handler)))
            if "create_order_route" in by_handler:
                route = by_handler["create_order_route"]
                check("路径为 /api/orders", route.path == "/api/orders", route.path)
                check("method 为 POST", route.method == "POST", route.method)
                check("来源标 framework_plugin", route.source == "framework_plugin", route.source)
                check("证据等级 verified", route.confidence == VERIFIED, route.confidence)
        finally:
            client.close()


def test_embedded_client() -> None:
    print("\n=== C2. 嵌入式客户端（@app.route 双源交叉）===")
    env = _require_env()
    if env is None:
        return
    module_root, node_bin = env

    from codegraph import EmbeddedCodeGraphClient  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_project(root, "route")

        client = EmbeddedCodeGraphClient(root, module_root=module_root, node=str(node_bin))
        try:
            index = client.ensure_index(full=True)
            check("索引成功", index.get("indexed") is True, str(index.get("reason")))

            stats = client.stats()
            check("框架识别到 flask", "flask" in (stats.get("frameworks") or []),
                  str(stats.get("frameworks")))
            check("文件数 >= 2", int(stats.get("fileCount", 0)) >= 2, str(stats.get("fileCount")))

            routes = client.find_routes()
            by_handler = {r.handler: r for r in routes}
            check("找到 create_order_route", "create_order_route" in by_handler,
                  str(list(by_handler)))
            if "create_order_route" in by_handler:
                route = by_handler["create_order_route"]
                check("路径为 /api/orders", route.path == "/api/orders", route.path)
                check("method 为 POST", route.method == "POST", route.method)
                check("证据等级 verified", route.confidence == VERIFIED, route.confidence)

            callees = client.callees("create_order")
            callee_names = {s.name for s in callees}
            check("正向调用拿到 reserve/charge",
                  {"reserve", "charge"} & callee_names == {"reserve", "charge"},
                  str(callee_names))

            result = client.impact("reserve", depth=3)
            check("影响半径含接口级结果",
                  any(r.handler == "create_order_route" for r in result.affected_routes),
                  f"affected={result.affected} routes={[r.handler for r in result.affected_routes]}")

            report = client.route_report
            check("路由审计报告已生成", report is not None)
            if report:
                print(f"        报告: {report.as_dict()}")
                check("图谱与源码交叉命中", report.matched >= 1, str(report.matched))
        finally:
            client.close()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _build_java_project(root: Path) -> None:
    write(
        root,
        "pom.xml",
        """
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <modelVersion>4.0.0</modelVersion>
          <groupId>com.example</groupId>
          <artifactId>demo</artifactId>
          <version>1.0.0</version>
          <dependencies>
            <dependency>
              <groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter-web</artifactId>
              <version>3.2.0</version>
            </dependency>
          </dependencies>
        </project>
        """,
    )
    write(
        root,
        "src/main/java/com/example/OrderController.java",
        """
        package com.example;

        import org.springframework.web.bind.annotation.*;

        @RestController
        @RequestMapping("/api/v1/orders")
        public class OrderController {
            private final OrderService service;

            public OrderController(OrderService service) {
                this.service = service;
            }

            @GetMapping("/{id}")
            public String get(@PathVariable String id) {
                return service.load(id);
            }

            @PostMapping
            public String create(@RequestBody String body) {
                return service.create(body);
            }
        }
        """,
    )
    write(
        root,
        "src/main/java/com/example/OrderService.java",
        """
        package com.example;

        import org.springframework.stereotype.Service;

        @Service
        public class OrderService {
            public String load(String id) {
                return helper(id);
            }

            public String create(String body) {
                return "ok";
            }

            private String helper(String id) {
                return id;
            }
        }
        """,
    )


def test_java_spring_branch_diff_to_api() -> None:
    """第一层闭环：Java 分支变更 → CodeGraph 符号 → 受影响 API。

    不是「方法级测试」：断言只要求最终得到 API 级影响列表；方法只是追溯的
    中间证据。CodeGraph 不可用时跳过，不伪装通过。
    """
    print("\n=== D. Java/Spring：分支变更 → 受影响 API ===")
    env = _require_env()
    if env is None:
        return
    module_root, node_bin = env

    from astsight.changes.git_diff import diff_files  # noqa: E402
    from astsight.changes.impact import analyze_impact  # noqa: E402
    from astsight.changes.symbol_diff import symbols_for_changes  # noqa: E402
    from codegraph import EmbeddedCodeGraphClient  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_java_project(root)
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "base")
        _git(root, "checkout", "-b", "feature")

        service = root / "src/main/java/com/example/OrderService.java"
        service.write_text(
            service.read_text(encoding="utf-8").replace(
                "return id;", 'return id + "x";'
            ),
            encoding="utf-8",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change")

        client = EmbeddedCodeGraphClient(
            root, module_root=module_root, node=str(node_bin)
        )
        try:
            index = client.ensure_index(full=True)
            assert index.get("indexed") is True, index.get("reason")
            assert "spring" in (client.stats().get("frameworks") or [])

            routes = client.find_routes()
            by_handler = {route.handler: route for route in routes}
            assert "get" in by_handler and "create" in by_handler
            assert by_handler["get"].path == "/api/v1/orders/{id}"
            assert by_handler["get"].method == "GET"

            nodes = client.symbols_in_file(
                "src/main/java/com/example/OrderService.java"
            )
            helper = next(node for node in nodes if node.name == "helper")
            assert helper.kind == "method"
            assert helper.end_line > helper.line
            assert helper.node_id

            changes, meta = diff_files(root, "main", "feature")
            assert meta["comparison"] == "three_dot"
            symbols, unsupported = symbols_for_changes(
                root, "main", "feature", changes, codegraph_client=client
            )
            assert not unsupported
            assert [(s.name, s.kind, s.evidence) for s in symbols] == [
                ("helper", "method", "verified")
            ]

            route_dicts = [
                {
                    "method": route.method,
                    "path": route.path,
                    "handler": route.handler,
                    "file": route.file,
                }
                for route in routes
            ]
            summary = analyze_impact(
                symbols, route_dicts, client, depth=5
            )
            apis = {(api["method"], api["path"]) for api in summary.changed_apis}
            assert apis == {("GET", "/api/v1/orders/{id}")}
            api = summary.changed_apis[0]
            assert api["confidence"] == "medium"
            assert api["evidence"] == "inferred"
        finally:
            client.close()


def main() -> int:
    test_flask_blueprint_prefix()
    test_django_include_prefix()
    test_plugin_fills_graph_miss()
    test_conflict_is_unmapped()
    test_graph_only_is_inferred()
    test_suffix_match_safety()
    test_embedded_client_shortcut_decorator()
    test_embedded_client()
    test_java_spring_branch_diff_to_api()

    print()
    if _FAILS:
        print(f"失败 {len(_FAILS)} 项：" + "、".join(_FAILS))
        return 1
    print("全部通过：路由前缀重建与证据等级成立，嵌入客户端可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
