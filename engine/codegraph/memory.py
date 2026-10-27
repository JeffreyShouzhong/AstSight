"""CodeGraphClient 的内存 stub 实现（测试替身）。

用于在真实图谱不可用（CI 无 Node、离线单测、构造边界用例）时，
验证 C2/C3 通过接口消费图谱数据的逻辑。
真实实现是 `EmbeddedCodeGraphClient`（代码嵌入），接口不变，可直接替换。
"""
from __future__ import annotations

from .client import (
    CodeGraphClient,
    ImpactResult,
    RouteRef,
    SymbolRef,
)


class InMemoryCodeGraphClient(CodeGraphClient):
    """基于内存数据的图谱客户端。

    由调用方注入"符号调用关系 + 路由表"，模拟 CodeGraph 的返回。
    """

    def __init__(
        self,
        call_edges: dict[str, list[str]] | None = None,
        routes: list[RouteRef] | None = None,
        languages: list[str] | None = None,
        symbols_by_file: dict[str, list[SymbolRef]] | None = None,
    ):
        # call_edges: caller -> [callees]
        self._call_edges: dict[str, list[str]] = call_edges or {}
        self._routes: list[RouteRef] = routes or []
        self._languages: list[str] = languages or ["python"]
        self._symbols_by_file: dict[str, list[SymbolRef]] = symbols_by_file or {}

        # 反向索引：callee -> [callers]
        self._callers: dict[str, list[str]] = {}
        for caller, callees in self._call_edges.items():
            for callee in callees:
                self._callers.setdefault(callee, []).append(caller)

    def impact(self, name: str, depth: int = 3, *, node_id: str = "") -> ImpactResult:
        """从 name 沿反向调用链向上追溯，直到路由 handler。

        内存替身没有真实节点 id，`node_id` 只作为接口兼容参数；按名追溯。
        """
        affected: list[str] = []
        trace: list[str] = [name]

        # BFS 从 name 向上（callee -> caller）
        frontier = [name]
        visited = {name}
        for _ in range(depth):
            nxt: list[str] = []
            for node in frontier:
                for caller in self._callers.get(node, []):
                    if caller not in visited:
                        visited.add(caller)
                        trace.append(caller)
                        nxt.append(caller)
            frontier = nxt
            if not frontier:
                break

        # 命中路由 handler 的，标记为 affected
        handlers = {r.handler: r for r in self._routes}
        affected = [s for s in trace if s in handlers]
        return ImpactResult(
            symbol=name,
            affected=affected,
            trace=trace,
            affected_routes=[handlers[s] for s in affected],
            depth_used=depth,
        )

    def callers(self, name: str) -> list[SymbolRef]:
        return [SymbolRef(name=c) for c in self._callers.get(name, [])]

    def callees(self, name: str) -> list[SymbolRef]:
        return [SymbolRef(name=c) for c in self._call_edges.get(name, [])]

    def find_routes(self) -> list[RouteRef]:
        return list(self._routes)

    def symbols_in_file(self, file_path: str) -> list[SymbolRef]:
        return list(self._symbols_by_file.get(file_path, []))

    def supported_languages(self) -> list[str]:
        return list(self._languages)
