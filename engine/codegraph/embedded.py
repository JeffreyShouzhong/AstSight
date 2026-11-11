"""CodeGraph 的「代码嵌入」客户端。

## 什么是代码嵌入（相对 MCP）

MCP 模式下，CodeGraph 以 server 形态对外提供工具，返回的是**面向模型阅读**的
内容（explore 的 markdown 包把源码、调用路径、爆炸半径混排在一起）。判定管道
要的是机器可解析、可复现、可审计的结构化事实，MCP 输出不符合。

代码嵌入模式：本框架的常驻 Node worker 进程**直接 import CodeGraph 库**，
每个方法的入参与返回结构由 AstSight 自己定义。协议是我们自己的逐行 JSON，
不是 MCP——没有 capability 协商、没有工具发现、没有面向模型的输出。

> 否决的是「协议层 + 面向模型的输出」，不是「跨进程」。Python 主进程与 Node
> worker 分属两个 runtime，这是不可消除的现实（CodeGraph 是 Node 库）。

## 进程模型

    Python 主进程
      └─ EmbeddedCodeGraphClient
           └─ node codegraph_worker.mjs   （常驻，一次启动多次调用）
                └─ CodeGraph 实例（一仓一实例，各自持有 .codegraph/）

为什么常驻而不是每次拉进程：CodeGraph 打开 SQLite 索引有秒级开销，
C2/C3 一次分析要发几十次调用，逐次冷启动不可接受。常驻 + 复用实例把
这部分成本摊到一次。

## 失败语义

任何失败都返回**降级结果并标注原因**，不抛未捕获异常打断整条分析：
- worker 启动失败 / Node 版本不足 → 抛 `CodeGraphUnavailable`（这是环境问题，
  应当早失败，而不是静默让所有分析退化成空）；
- 单个调用失败 → 返回空结果 + `last_error`，调用方据此标 `unsupported`。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from .client import (
    CodeGraphClient,
    ImpactResult,
    RouteRef,
    SymbolRef,
)
from .route_repair import (
    RoutePrefixRepair,
    RouteRepairReport,
    normalize_django_path,
)
from .runtime import resolve_module_root

_DEFAULT_TIMEOUT = 60.0
_INDEX_TIMEOUT = 900.0  # 全量索引大仓可能十几分钟
_BRIDGE_DIR = Path(__file__).resolve().parent / "bridge"
_WORKER_SCRIPT = _BRIDGE_DIR / "codegraph_worker.mjs"


class CodeGraphUnavailable(RuntimeError):
    """worker 无法启动（无 node / 版本过低 / 包未安装）。

    这是环境级故障，必须早失败——否则整条管道会安静地得到空图谱，
    所有接口都判"无影响"，看起来一切正常，实际什么都没测。
    """


def _find_node() -> str:
    """定位 Node 可执行文件。

    优先托管版本（隔离、版本可控），其次系统 PATH。
    """
    managed = Path(os.environ.get("ASTSIGHT_NODE", ""))
    if managed.is_file():
        return str(managed)

    candidates = sorted(
        Path.home().glob(".workbuddy/binaries/node/versions/*/node.exe"),
        reverse=True,
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    raise CodeGraphUnavailable(
        "找不到 node 可执行文件。CodeGraph 库模式要求 Node >= 22.5（依赖 node:sqlite）。"
        "请安装 Node 或设置 ASTSIGHT_NODE 指向可执行文件。"
    )


class EmbeddedCodeGraphClient(CodeGraphClient):
    """通过常驻 Node worker 内嵌调用 CodeGraph 库。"""

    def __init__(
        self,
        project_root: str | Path,
        *,
        module_root: str | Path | None = None,
        node: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        repair_routes: bool = True,
        max_consecutive_failures: int = 3,
    ):
        self.project_root = str(Path(project_root).resolve())
        # 显式参数 → 环境变量 → 用户级持久化目录（codegraph install 安装）。
        self.module_root = resolve_module_root(module_root)
        self._node = node or _find_node()
        self._timeout = timeout
        self._repair_routes = repair_routes
        self._max_failures = max_consecutive_failures

        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._seq = 0
        self._failures = 0
        self._ready_info: dict[str, Any] = {}
        self._last_error: str = ""
        self._route_report: RouteRepairReport | None = None
        # 路由表在一次 C2 里会被 impact() 反复求交；Python 路由修复要扫源码，
        # 不缓存会把「几十个变更符号」放大成「几十次全仓扫描」。
        self._routes_cache: list[RouteRef] | None = None

    # ---------------------------------------------------------------- 生命周期

    def _start(self) -> None:
        """启动 worker 并完成 ready 握手。"""
        if self._proc and self._proc.poll() is None:
            return

        env = os.environ.copy()
        if self.module_root:
            env["ASTSIGHT_CODEGRAPH_MODULE_ROOT"] = self.module_root
        # 企业仓库场景，遥测默认必须关（设计基线工程红线）
        env.setdefault("CODEGRAPH_TELEMETRY", "0")
        env.setdefault("DO_NOT_TRACK", "1")

        try:
            self._proc = subprocess.Popen(
                [self._node, str(_WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
                cwd=self.project_root,
            )
        except OSError as exc:
            raise CodeGraphUnavailable(f"无法启动 CodeGraph worker: {exc}") from exc

        # 首行必须是 ready 事件；拿不到说明 worker 在 import 阶段就崩了
        first = self._read_line(block=True)
        if first is None:
            detail = self._drain_stderr()
            self._terminate()
            raise CodeGraphUnavailable(
                "CodeGraph worker 启动即退出。" + (f"stderr: {detail}" if detail else "")
            )

        try:
            handshake = json.loads(first)
        except json.JSONDecodeError as exc:
            self._terminate()
            raise CodeGraphUnavailable(f"worker 握手输出非 JSON: {first!r}") from exc

        if handshake.get("event") != "ready":
            self._terminate()
            raise CodeGraphUnavailable(f"worker 握手异常: {handshake}")

        if not handshake.get("nodeOk"):
            self._terminate()
            raise CodeGraphUnavailable(
                f"Node 版本 {handshake.get('node')} 不满足库模式要求（>= 22.5）。"
                "库模式依赖内置 node:sqlite。"
            )
        self._ready_info = handshake

    def _read_line(self, block: bool = True, timeout: float | None = None) -> str | None:
        """读 worker 的一行输出，带超时。

        用后台线程投递到队列：Windows 上 select 对管道无效，
        只能靠线程 + 队列实现可中断的读取。
        """
        assert self._proc and self._proc.stdout
        result: queue.Queue[str | None] = queue.Queue(maxsize=1)

        def reader() -> None:
            try:
                line = self._proc.stdout.readline()  # type: ignore[union-attr]
            except (ValueError, OSError):
                line = ""
            result.put(line or None)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            return result.get(timeout=timeout if timeout is not None else self._timeout)
        except queue.Empty:
            return None

    def _drain_stderr(self) -> str:
        if not self._proc or not self._proc.stderr:
            return ""
        try:
            return (self._proc.stderr.read() or "").strip()[:2000]
        except (ValueError, OSError):
            return ""

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass

    def close(self) -> None:
        self._terminate()

    def __enter__(self) -> "EmbeddedCodeGraphClient":
        self._start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 调用

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发一次请求。

        调用失败不抛异常，返回 `{"ok": False, "error": ...}`：图谱是分析的
        辅助证据源，单次失败不应中断整条管道，应由调用方标 unsupported。
        """
        payload_params = dict(params or {})
        payload_params.setdefault("root", self.project_root)

        with self._lock:
            try:
                self._start()
            except CodeGraphUnavailable as exc:
                self._last_error = str(exc)
                return {"ok": False, "error": {"code": "unavailable", "message": str(exc)}}

            self._seq += 1
            request = {"id": self._seq, "method": method, "params": payload_params}

            assert self._proc and self._proc.stdin
            try:
                self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._last_error = f"worker 写入失败: {exc}"
                self._terminate()
                return {"ok": False, "error": {"code": "worker_dead", "message": self._last_error}}

            raw = self._read_line(timeout=timeout if timeout is not None else self._timeout)
            if raw is None:
                self._failures += 1
                self._last_error = f"{method} 超时（>{timeout or self._timeout}s）"
                if self._failures >= self._max_failures:
                    # 连续超时：worker 可能已僵死，杀掉等下次重建
                    self._terminate()
                    self._failures = 0
                return {"ok": False, "error": {"code": "timeout", "message": self._last_error}}

            try:
                response = json.loads(raw)
            except json.JSONDecodeError:
                self._failures += 1
                self._last_error = f"{method} 返回非 JSON: {raw[:200]!r}"
                return {"ok": False, "error": {"code": "bad_response", "message": self._last_error}}

            if not response.get("ok"):
                self._failures += 1
                error = response.get("error") or {}
                self._last_error = f"{method}: {error.get('code')} {error.get('message')}"
                return {"ok": False, "error": error}

            self._failures = 0
            return {"ok": True, "result": response.get("result") or {}}

    def _result(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict:
        response = self.call(method, params, **kw)
        return response.get("result", {}) if response.get("ok") else {}

    @property
    def last_error(self) -> str:
        """最近一次失败原因，进报告用。空串表示上次调用成功。"""
        return self._last_error

    @property
    def route_report(self) -> RouteRepairReport | None:
        return self._route_report

    # ---------------------------------------------------------- 核心能力实现

    def ensure_index(self, full: bool = False) -> dict:
        # 索引一变，旧路由表与文件内符号都可能过期。
        self._routes_cache = None
        result = self._result(
            "index.ensure",
            {"full": full},
            timeout=_INDEX_TIMEOUT if full else max(self._timeout, 180.0),
        )
        self._routes_cache = None
        return result

    def stats(self) -> dict:
        result = self._result("stats")
        return {
            "project_root": result.get("projectRoot", self.project_root),
            "frameworks": result.get("frameworks", []),
            **(result.get("stats") or {}),
        }

    def supported_languages(self) -> list[str]:
        """CodeGraph 支持的语言取自包内的静态表（LANGUAGES 常量）。

        运行时不提供查询接口，因此这里返回**锁定版本的静态表**，
        并在 `source` 上标明它不是运行时探测结果。
        """
        return [
            "typescript", "javascript", "tsx", "jsx", "arkts", "python", "go",
            "rust", "java", "c", "cpp", "csharp", "razor", "php", "ruby",
            "swift", "kotlin", "dart", "svelte", "vue", "astro", "liquid",
            "pascal", "scala", "lua", "luau", "objc", "r", "solidity", "nix",
            "yaml", "twig", "xml", "properties", "cfml", "cfscript", "cfquery",
            "cobol", "vbnet", "erlang", "terraform", "unknown",
        ]

    def _node_ids(self, name: str) -> list[SymbolRef]:
        """按名取符号。先精确名，回退 FTS 检索——FTS 会把 `getProfileInfoV2`
        切成多个 token 导致漏召回，精确名兜住这类情况。"""
        exact = self._result("nodes.byname", {"name": name})
        symbols = [_to_symbol(item) for item in exact.get("nodes", [])]
        if symbols:
            return symbols
        searched = self._result("search", {"query": name, "limit": 10})
        return [_to_symbol(m.get("symbol") or {}) for m in searched.get("matches", [])]

    def search(self, query: str, limit: int = 10) -> list[SymbolRef]:
        result = self._result("search", {"query": query, "limit": limit})
        return [_to_symbol(m.get("symbol") or {}) for m in result.get("matches", [])]

    def node_by_name(self, name: str) -> list[SymbolRef]:
        return self._node_ids(name)

    def callers(self, name: str) -> list[SymbolRef]:
        symbols = self._node_ids(name)
        if not symbols:
            return []
        result = self._result("callers", {"nodeId": symbols[0].node_id, "depth": 1})
        return _symbols_from_edges(result.get("edges", []))

    def callees(self, name: str) -> list[SymbolRef]:
        symbols = self._node_ids(name)
        if not symbols:
            return []
        result = self._result("callees", {"nodeId": symbols[0].node_id, "depth": 1})
        return _symbols_from_edges(result.get("edges", []))

    def impact(self, name: str, depth: int = 3, *, node_id: str = "") -> ImpactResult:
        if node_id:
            # 调用方从文件符号表拿到的是精确节点；按名反查会在 Java 同名重载
            # 之间选错节点，闭包自然也就追错。
            symbols = [SymbolRef(name=name, node_id=node_id)]
        else:
            symbols = self._node_ids(name)
        if not symbols:
            return ImpactResult(symbol=name, depth_used=depth)

        result = self._result(
            "impact", {"nodeId": symbols[0].node_id, "depth": depth}
        )
        if not result:
            return ImpactResult(symbol=name, depth_used=depth)

        affected = [_to_symbol(item) for item in result.get("nodes", [])]
        depth_used = int(result.get("depthUsed", depth))
        # 新 worker 显式返回 truncated；旧 worker 没有该字段时保留原保守口径。
        truncated = bool(
            result.get("truncated")
            if "truncated" in result
            else (len(affected) > 0 and depth_used >= depth)
        )

        # 符号级影响 → 接口级影响：与路由 handler 求交。
        # 不做这一步，C2 的"影响 3 个接口"就会变成"影响 40 个符号"，
        # 门禁的召回率失去意义。
        #
        # 不能只按名字建索引：Java 不同 Controller 出现同名 handler 很常见，
        # 名字相同但文件不同的不能算同一条路由。文件信息齐全时按文件消歧；
        # 只有双方都没有文件信息时才退回按名匹配。
        route_handlers: dict[str, list[RouteRef]] = {}
        for route in self.find_routes():
            if route.handler:
                route_handlers.setdefault(route.handler, []).append(route)

        affected_routes: list[RouteRef] = []
        for symbol in affected:
            candidates = route_handlers.get(symbol.name, [])
            if symbol.file and candidates:
                with_file = [route for route in candidates if route.file]
                if with_file:
                    same_file = [
                        route for route in with_file
                        if _same_file(route.file, symbol.file)
                    ]
                    candidates = same_file
            for route in candidates:
                if route not in affected_routes:
                    affected_routes.append(route)

        trace: list[str] = []
        for item in [symbols[0].qualified_name or symbols[0].name] + [
            s.name for s in affected
        ]:
            if item not in trace:
                trace.append(item)

        return ImpactResult(
            symbol=name,
            affected=[s.name for s in affected],
            trace=trace,
            affected_routes=affected_routes,
            depth_used=depth_used,
            # 深度打满说明闭包可能没走完，证据等级不能给 verified
            truncated=truncated,
        )

    def find_routes(self) -> list[RouteRef]:
        """取路由，并按需做前缀重建。

        CodeGraph 的路由产出有两个静默缺陷（实测 1.6.2）：
        Flask Blueprint 的 url_prefix、Django include() 的前缀都不会拼接，
        Django 还完全没有 HTTP method。直接拿来用会打到缺前缀的 URL，
        得到 404 并把失败归因到无关模块——这是最典型的"测试假红"。

        因此路由一律经过 RoutePrefixRepair 交叉校验后才对外返回，
        `confidence` 与 `source` 如实反映来源。
        """
        if self._routes_cache is not None:
            return list(self._routes_cache)

        result = self._result("routes", {"limit": 5000})
        graph_routes: list[RouteRef] = []
        for entry in result.get("entries", []):
            raw = entry.get("url", "")
            method, path = _split_route_name(raw)
            graph_routes.append(
                RouteRef(
                    method=method,
                    path=path,
                    handler=entry.get("handler", ""),
                    file=entry.get("handlerFile", ""),
                    line=int(entry.get("handlerLine", 0) or 0),
                    route_file=entry.get("routeFile", ""),
                    route_line=int(entry.get("routeLine", 0) or 0),
                    note=(
                        "drf_viewset_route"
                        if raw.strip().upper().startswith("VIEWSET ")
                        else ""
                    ),
                )
            )

        if not self._repair_routes:
            self._routes_cache = graph_routes
            return list(graph_routes)

        repair = RoutePrefixRepair(self.project_root)
        routes = repair.repair(graph_routes)
        self._route_report = repair.last_report
        self._routes_cache = routes
        return list(routes)

    def symbols_in_file(self, file_path: str) -> list[SymbolRef]:
        """列出文件内全部节点（含方法/类/字段的行区间）。

        行区间来自 CodeGraph 的提取器，不在 AstSight 里做正则或缩进推断：
        这是 C2 能支持 Java/Kotlin/TS 等语言而不变成「半个解析器」的前提。
        """
        result = self._result("nodes.infile", {"filePath": file_path})
        return [_to_symbol(item) for item in result.get("nodes", [])]

    def context(self, query: str, max_nodes: int = 20, include_code: bool = True) -> str:
        """上下文包。**只给人和 Agent 读，禁止进确定性判定管道。**"""
        result = self._result(
            "context",
            {"query": query, "maxNodes": max_nodes, "includeCode": include_code},
        )
        return result.get("markdown", "")

    def graph_health(self) -> dict:
        return self._result("graph.health")


# ----------------------------------------------------------------- 工具函数


def _same_file(left: str, right: str) -> bool:
    def normalize(path: str) -> str:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    return normalize(left) == normalize(right)


def _to_symbol(payload: dict[str, Any]) -> SymbolRef:
    return SymbolRef(
        name=payload.get("name", ""),
        kind=payload.get("kind", ""),
        file=payload.get("file", ""),
        line=int(payload.get("line", 0) or 0),
        end_line=int(payload.get("endLine", 0) or 0),
        signature=payload.get("signature", "") or "",
        qualified_name=payload.get("qualifiedName", "") or "",
        node_id=payload.get("id", "") or "",
        language=payload.get("language", "") or "",
    )


def _symbols_from_edges(edges: list[dict[str, Any]]) -> list[SymbolRef]:
    return [_to_symbol(edge.get("symbol") or {}) for edge in edges if edge.get("symbol")]


_ROUTE_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _split_route_name(raw: str) -> tuple[str, str]:
    """把 route 节点的 name 拆成 (method, path)。

    Flask/FastAPI 的 route 节点 name 形如 `GET /api/orders`；
    Django 的 name 只有路径（`orders/`），没有 method —— 此时 method 为空串，
    这是如实反映，不做猜测。

    CodeGraph 对 DRF 会产出 `VIEWSET /articles`：`VIEWSET` 不是 HTTP method，
    必须剥掉并保留 method 为空，否则会生成一条永远请求不到的假接口。
    Django 的原始正则路径（`^articles/feed/?$`）在这里统一规范化。
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    head, sep, rest = text.partition(" ")
    upper = head.upper()
    if sep and upper in _ROUTE_HTTP_METHODS:
        return upper, normalize_django_path(rest)
    if upper == "VIEWSET":
        return "", normalize_django_path(rest or text)
    return "", normalize_django_path(text)
