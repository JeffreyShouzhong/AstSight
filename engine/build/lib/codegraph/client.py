"""CodeGraph 客户端接口抽象。

对齐代码图谱操作抽象（impact/callers/callees），让 C2（反向追溯）和
C3（正向追溯）只依赖这个接口，底层实现透明。

接入方式（v2.0 起）：
- **代码嵌入**：`EmbeddedCodeGraphClient` 在常驻 Node 进程内直接 import
  `@colbymchenry/codegraph` 库，经 stdio JSON 逐行通信。这是唯一正式路径。
- **MCP 已否决**：不进 C2/C3/C5。理由是 MCP 的 explore 返回面向模型阅读的
  markdown 包（源码 + 调用路径 + 爆炸半径混排），而事实源环节要求机器可解析、
  可复现、可审计。否决的是"协议层 + 面向模型的输出"，不是"跨进程"本身。

设计原则：
- 框架面向"图谱操作"编程，不绑定某个具体图谱实现；
- 语言不支持、索引缺失时返回空并标 `unsupported`，调用方据此跳过，
  **不做降级猜测**（宁可漏、不可错）；
- 路由路径一律视为**待校验**：CodeGraph 实测会丢失框架前缀（见 route_repair），
  任何回填都必须携带证据等级。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# 证据等级，与设计基线全文口径一致
VERIFIED = "verified"
INFERRED = "inferred"
UNMAPPED = "unmapped"


@dataclass
class SymbolRef:
    """符号引用结果。"""

    name: str
    kind: str = ""           # func/method/type/var/const/field/route/class
    file: str = ""
    line: int = 0
    end_line: int = 0        # 符号结束行；行区间→符号映射必须用到
    signature: str = ""      # 签名/上下文
    qualified_name: str = ""
    node_id: str = ""        # 图谱节点 id，可直接喂给 callers/callees/impact
    language: str = ""


@dataclass
class RouteRef:
    """路由引用结果。

    `method` 与 `path` 都可能不完整来源：
    - Django 路由天然没有 HTTP method（method 在 view 内部按 request.method 分支），
      此时 `method` 为空字符串，调用方须标 `unmapped` 或走框架插件补；
    - Flask Blueprint / Django include 的前缀不会由 CodeGraph 拼接，
      `path` 是去前缀的裸路径，需 route_repair 回填。
    因此 `source` 与 `confidence` 是路由的**必备元数据**，不是可选注解。
    """

    method: str              # GET/POST/...（空串 = 图谱未给出）
    path: str                # /api/orders（可能是去前缀裸路径）
    handler: str             # handler 符号名
    handler_owner: str = ""  # 类方法路由：所属类名（Django APIView/ViewSet 用）
    file: str = ""
    line: int = 0
    handler_id: str = ""     # 图谱节点 id
    route_file: str = ""     # 路由声明所在文件（与 handler 文件可能不同）
    route_line: int = 0
    source: str = "graph"    # graph | graph_repaired | framework_plugin
    confidence: str = INFERRED
    note: str = ""           # 附加说明，如 graph_missed
    conflict_with: str = ""  # 冲突时记录源码侧的重建路径，供人工复核


@dataclass
class ImpactResult:
    """变更影响分析结果。

    `affected` 是符号名列表（兼容旧口径）；`affected_routes` 是求交后才得到的
    接口级结果——**符号级影响不能直接当接口级影响用**，否则 C2 的召回率门禁无意义。
    """

    symbol: str
    affected: list[str] = field(default_factory=list)   # 受影响符号（含路由 handler）
    trace: list[str] = field(default_factory=list)      # 追溯链
    affected_routes: list[RouteRef] = field(default_factory=list)
    depth_used: int = 0
    truncated: bool = False     # 是否因深度上限被截断（→ 证据等级降为 inferred）


class CodeGraphClient(ABC):
    """代码图谱客户端接口。

    C2/C3 只依赖此接口。返回为空列表代表"未找到"或"语言不支持"，
    调用方据此标 unsupported，不做降级猜测。
    """

    # ---- 核心能力（必须由实现提供）----

    @abstractmethod
    def impact(self, name: str, depth: int = 3, *, node_id: str = "") -> ImpactResult:
        """改某符号牵连哪些下游（传递闭包 / 爆炸半径）。

        `name` 是符号名，供按名反查；仅当调用方已经持有图谱节点 id 时传
        `node_id`，以避开同名重载（Java 尤其常见）被错误合并。
        """

    @abstractmethod
    def callers(self, name: str) -> list[SymbolRef]:
        """谁调用了某符号（C2 反向追溯用）。"""

    @abstractmethod
    def callees(self, name: str) -> list[SymbolRef]:
        """某符号调用了哪些（C3 正向识别用）。"""

    @abstractmethod
    def find_routes(self) -> list[RouteRef]:
        """找出全部路由 → handler 映射（C3 接口画像用）。"""

    @abstractmethod
    def supported_languages(self) -> list[str]:
        """声明支持的编程语言（供调用方判断是否 skip）。"""

    # ---- 扩展能力（有默认降级实现，实现类按需覆盖）----

    def search(self, query: str, limit: int = 10) -> list[SymbolRef]:
        """符号检索。默认不支持 → 空列表（调用方视为 unsupported）。"""
        return []

    def node_by_name(self, name: str) -> list[SymbolRef]:
        """精确名查找（枚举同名重载）。默认退化为 search。"""
        return self.search(name, limit=20)

    def symbols_in_file(self, file_path: str) -> list[SymbolRef]:
        """列出某文件内全部符号（含行区间）。

        C2 的非 Python 变更行→符号映射依赖它；默认返回空列表，调用方必须
        据此标 `unsupported`，不能自己用正则或缩进猜符号边界。
        """
        return []

    def ensure_index(self, full: bool = False) -> dict:
        """建立/刷新索引。默认无操作（内存实现无需索引）。"""
        return {"indexed": False, "reason": "not_supported"}

    def stats(self) -> dict:
        """索引元信息（文件数/节点数/框架检测结果）。默认空。"""
        return {}

    def context(self, query: str, max_nodes: int = 20, include_code: bool = True) -> str:
        """面向人或 Agent 的上下文包（markdown）。

        **禁止**用于确定性判定管道——判定只读结构化结果。
        默认返回空串。
        """
        return ""

    def graph_health(self) -> dict:
        """图健康度（依赖环、高被依赖符号）。默认空。"""
        return {}
