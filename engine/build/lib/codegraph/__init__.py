"""CodeGraph 客户端公共导出。

正式路径是代码嵌入（`EmbeddedCodeGraphClient`）：
常驻 Node worker 进程内直接 import `@colbymchenry/codegraph` 库。
MCP 不进 C2/C3/C5 管道（理由见 `client.py` 模块注释）。

`InMemoryCodeGraphClient` 保留为测试替身：接口不变，
上层 S2/S3 逻辑的单元测试不依赖真实图谱。
"""
from .client import (
    INFERRED,
    UNMAPPED,
    VERIFIED,
    CodeGraphClient,
    ImpactResult,
    RouteRef,
    SymbolRef,
)
from .embedded import CodeGraphUnavailable, EmbeddedCodeGraphClient
from .memory import InMemoryCodeGraphClient
from .route_repair import RouteDeclaration, RoutePrefixRepair, RouteRepairReport
from .runtime import (
    CODEGRAPH_PACKAGE,
    CODEGRAPH_VERSION,
    default_module_root,
    install,
    installed_version,
    is_installed,
    resolve_module_root,
    status,
)

__all__ = [
    "CODEGRAPH_PACKAGE",
    "CODEGRAPH_VERSION",
    "default_module_root",
    "install",
    "installed_version",
    "is_installed",
    "resolve_module_root",
    "status",
    "CodeGraphClient",
    "EmbeddedCodeGraphClient",
    "CodeGraphUnavailable",
    "InMemoryCodeGraphClient",
    "ImpactResult",
    "RouteRef",
    "SymbolRef",
    "RoutePrefixRepair",
    "RouteDeclaration",
    "RouteRepairReport",
    "VERIFIED",
    "INFERRED",
    "UNMAPPED",
]
