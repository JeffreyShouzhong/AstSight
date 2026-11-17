"""被测 Dubbo 服务的调用客户端（sidecar）。

sidecar 是独立 Java 进程：引擎把调用写成 JSONL 送进 stdin，sidecar 用
`ReferenceConfig<GenericService>` 泛化调用目标服务，再把结构化结果写回
stdout。它不 Attach 被测 JVM、不加载 java agent，也不执行仓库代码。
"""
from .dubbo import (
    SIDECAR_PROTOCOL,
    DubboCallResult,
    DubboSidecar,
    DubboSidecarError,
    locate_java,
    locate_sidecar_jar,
)

__all__ = [
    "SIDECAR_PROTOCOL",
    "DubboCallResult",
    "DubboSidecar",
    "DubboSidecarError",
    "locate_java",
    "locate_sidecar_jar",
]
