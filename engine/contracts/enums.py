"""框架全局枚举定义。

对齐 `framework-capability-design.md` v2.15 的口径，是契约层的公共基础。
所有实体字段引用这里的枚举，避免各模块各自硬编码字符串。
"""
from enum import Enum


class Evidence(str, Enum):
    """证据等级（三级，对齐 CodeGraph 诚实标记 FACT/POSSIBLE/UNKNOWN）。

    - verified : 确定性事实，可被 AST/元数据证明
    - inferred : 推断但未证实（动态分发、别名、命名启发式等）
    - unmapped : 无法判定/未映射，不得臆造，须人工介入
    """

    VERIFIED = "verified"
    INFERRED = "inferred"
    UNMAPPED = "unmapped"


class Confidence(str, Enum):
    """变更感知置信度（C2 输出）。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Priority(str, Enum):
    """用例优先级（人工审核/用例排序使用）。"""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class AssertStrength(str, Enum):
    """断言强度档。用例声明自己能判到什么程度，不把 L1 冒充成业务判定。"""

    L1 = "L1"  # 状态码 / 字段存在
    L2 = "L2"  # 确定值
    L3 = "L3"  # 关系式
    L4 = "L4"  # 跨实体不变量


class CaseStatus(str, Enum):
    """用例在项目级 case store 中的状态。"""

    APPROVED = "approved"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class ReviewDecision(str, Enum):
    """人工审核决定；pending 未解析时不得写库。"""

    PENDING = "pending"
    APPROVE = "approve"
    REJECT = "reject"


class RootCause(str, Enum):
    """A5 五类根因（动作归属视角）。"""

    CODE_DEFECT = "code_defect"      # 真实代码缺陷 → user
    CASE_ERROR = "case_error"        # 用例/断言写错 → user
    DATA_ERROR = "data_error"        # 造数能力缺口 → framework
    MOCK_ERROR = "mock_error"        # Mock 语义模型覆盖不足 → framework
    CHANGE_INDUCED = "change_induced"  # 变更导致行为变化 → user


class ActionTarget(str, Enum):
    """根因动作归属路由（A5 输出，供报告合并器消费）。"""

    USER = "user"            # 反馈用户改进（改用例/修代码）
    FRAMEWORK = "framework"  # 回流框架升级（升级造数/Mock 能力）


class GraphSource(str, Enum):
    """图谱来源溯源（S2/S3 输出，回答"这次追溯用哪个图谱源"）。"""

    CODEGRAPH_MCP = "codegraph_mcp"


class SymbolKind(str, Enum):
    """代码符号种类。"""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
