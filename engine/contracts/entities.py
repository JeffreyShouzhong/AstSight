"""核心实体定义（对齐能力文档 v2.15 数据模型总览）。

每个实体字段都带证据等级（evidence）或溯源（source），
保证"事实源环节零 LLM、不臆造"可被机器审计。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import SourceRef
from .enums import (
    ActionTarget,
    AssertStrength,
    CaseLevel,
    Confidence,
    CoverageRole,
    Evidence,
    PreconditionKind,
    Priority,
    RootCause,
    RuleAuthority,
    RuleSourceKind,
    Transport,
)


# ───────────────────────── ChangedAPI（变更接口）─────────────────────────

class ChangedAPI(BaseModel):
    """变更接口（C2/S2 产出，S3/C4 消费）。"""

    model_config = ConfigDict(extra="forbid")

    handler: str = Field(description="如 'POST /api/orders'")
    confidence: Confidence = Field(description="high/medium/low")
    trace: list[str] = Field(
        default_factory=list,
        description="变更符号到 handler 的追溯链（含两端）",
    )
    changed_symbols: list[str] = Field(default_factory=list, description="命中变更的符号")
    evidence: Evidence = Field(default=Evidence.UNMAPPED, description="证据等级")


# ───────────────────────── Profile（接口画像）─────────────────────────

class Constraint(BaseModel):
    """字段/参数约束（三路校验取最严后落定）。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="字段名")
    constraint: str = Field(description="约束描述，如 'qty > 0'")
    evidence: Evidence = Field(default=Evidence.UNMAPPED)


class DBLineage(BaseModel):
    """DB 血缘边（C3 产出，C5 消费）。"""

    model_config = ConfigDict(extra="forbid")

    edge: str = Field(
        description="READS_TABLE/READS_COLUMN/WRITES_TABLE/WRITES_COLUMN/MAPS_TO_TABLE/MAPS_TO_COLUMN/FOREIGN_KEY_TO",
    )
    table: str = Field(description="表名")
    column: str | None = Field(default=None, description="列名（列级血缘时）")
    evidence: Evidence = Field(default=Evidence.UNMAPPED)


class DownstreamDep(BaseModel):
    """下游依赖（C3 识别）。"""

    model_config = ConfigDict(extra="forbid")

    dep_id: str = Field(
        description="下游依赖标识；HTTP 为 http:{downstream_key} {METHOD} {path}（C3.5.5）"
    )
    dep_type: str = Field(description="http/mq/redis/rpc/sdk")
    evidence: Evidence = Field(default=Evidence.UNMAPPED)


class Profile(BaseModel):
    """接口画像（C3/S3 产出，C4/C5 消费）。"""

    model_config = ConfigDict(extra="forbid")

    handler: str = Field(description="如 'POST /api/orders'")
    transport: Transport = Field(default=Transport.HTTP)
    business_key: str = Field(default="", description="业务身份字段/默认 handler")
    coverage_role: CoverageRole = Field(default=CoverageRole.PRIMARY)
    adapter_fingerprint: str | None = Field(
        default=None, description="适配器指纹；源码适配器可填，无则为空"
    )
    semantic_branches: list[str] = Field(
        default_factory=list, description="C3.5.1 业务语义分支"
    )
    params: list[Constraint] = Field(default_factory=list, description="入参约束")
    db_lineage: list[DBLineage] = Field(default_factory=list, description="DB 读写血缘")
    db_mapping: dict[str, str] = Field(
        default_factory=dict, description="字段 → 表/列 映射"
    )
    downstream_deps: list[DownstreamDep] = Field(default_factory=list, description="下游依赖")
    contract_incomplete: bool = Field(
        default=False,
        description="OpenAPI 缺失导致契约不完整（不降级猜测）",
    )


# ───────────────────────── DataPlan（造数计划）─────────────────────────

class DataPlan(BaseModel):
    """造数计划（C5b 产出，C6 消费）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1)
    plan_id: str = Field(default="")
    catalog_id: str = Field(default="")
    dialect: str = Field(default="sqlite", description="SQL 方言（sqlite/postgres/mysql）")
    ruleset_hash: str = Field(default="")
    case_id: str | None = Field(default=None)
    case_key: str = Field(default="")
    run_scope: str = Field(default="")
    tables_order: list[str] = Field(default_factory=list, description="拓扑序插入顺序")
    fixtures: dict[str, Any] = Field(
        default_factory=dict, description="表 → 行/字段取值（由 render 校验）"
    )
    target_state: dict[str, Any] = Field(
        default_factory=dict,
        description="用例目标状态（字段 → 目标值），业务态构造核心",
    )
    fk_links: list[str] = Field(
        default_factory=list, description="外键关联（优先取自 DB 血缘）"
    )
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    teardown: str | list[Any] = Field(
        default="delete_by_primary_key", description="清理策略/语句（按 fixtures 主键参数化删除）"
    )
    statements: list[dict[str, Any]] = Field(
        default_factory=list, description="参数化 SQL：{sql, params}"
    )
    warnings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


# ───────────────────────── 用例归属（规则/变更/前置状态）─────────────────────────

class RuleRef(BaseModel):
    """用例引用的规则快照；`rule_key` 是稳定身份。"""

    model_config = ConfigDict(extra="forbid")

    rule_key: str = Field(min_length=1, max_length=64)
    rule_id: str = Field(default="", max_length=64)
    authority: RuleAuthority = Field(default=RuleAuthority.B)
    source_kind: RuleSourceKind = Field(default=RuleSourceKind.EXPLICIT)


class ChangeRef(BaseModel):
    """代码变更归属；不进入指纹（换分支不重审）。"""

    model_config = ConfigDict(extra="forbid")

    base: str = Field(default="")
    target: str = Field(default="")
    symbols: list[str] = Field(default_factory=list, max_length=200)
    changed_api: str = Field(default="", max_length=512)


class Precondition(BaseModel):
    """用例前置数据/API 状态；state_key 必须能在 data_catalog 中找到。"""

    model_config = ConfigDict(extra="forbid")

    kind: PreconditionKind = Field(default=PreconditionKind.DB_STATE)
    state_key: str = Field(min_length=1, max_length=128)
    table: str | None = Field(default=None, max_length=128)
    where: dict[str, Any] = Field(default_factory=dict)
    set: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence = Field(default=Evidence.INFERRED)


# ───────────────────────── TestCase（用例，C4 产出 / C6 消费）─────────────────────────

class Assertion(BaseModel):
    """一条可执行断言（C4 产出，C6 编码，C7 判定）。

    支持 status_code / body_field / header / body_schema / db_row：
    - `body_schema`：按 OpenAPI 响应 JSON Schema 校验 body，target=状态码；
    - `db_row`：查询数据库验证落库状态，target=`table` 或 `table.column`；
    - `where` 仅用于 db_row，值可以是 `$ref`，执行时解析。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        description="status_code / body_field / header / body_schema / db_row"
    )
    target: str | None = Field(default=None, description="字段名、header 名、状态码或表名")
    op: str = Field(
        default="eq",
        description="eq/ne/in/contains/exists/absent/gte/lte/count_eq/matches",
    )
    expect: Any = Field(default=None, description="期望值；body_schema 为 JSON Schema")
    where: dict[str, Any] = Field(
        default_factory=dict, description="db_row 的 WHERE 等值条件"
    )


class CaseStep(BaseModel):
    """多请求用例中的一步；`method/path` 由 context 回填，不接受草案自报。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="步骤名，用例内唯一；用于响应引用")
    endpoint: str = Field(description="接口标识，如 'GET /api/v1/products'")
    method: str = Field(description="HTTP 方法")
    path: str = Field(description="请求路径（含路径参数占位）")
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default=None, description="请求体（JSON）")
    asserts: list[Assertion] = Field(default_factory=list)


class TestCase(BaseModel):
    """用例的数据形态（不落代码），C4 产出、C6 编译为 pytest。

    证据口径：`source` 必须指向真实输入（OpenAPI / 路由 / 需求文档），
    `assert_strength` 如实标注 L1–L4，模板生成的用例默认 L1。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="用例编号，如 TC-0001")
    title: str = Field(default="", description="用例标题（人读）")
    level: CaseLevel = Field(default=CaseLevel.BUSINESS)
    transport: Transport = Field(default=Transport.HTTP)
    coverage_role: CoverageRole = Field(default=CoverageRole.PRIMARY)
    rule_refs: list[RuleRef] = Field(default_factory=list)
    change_refs: list[ChangeRef] = Field(default_factory=list)
    preconditions: list[Precondition] = Field(default_factory=list)
    endpoint: str = Field(description="接口标识，如 'GET /api/v1/products'")
    category: str = Field(
        description="positive / missing_required / type_error / boundary / business"
    )
    method: str = Field(description="HTTP 方法")
    path: str = Field(description="请求路径（含路径参数占位）")
    priority: Priority = Field(default=Priority.P1)
    requirement_refs: list[SourceRef] = Field(
        default_factory=list, description="需求溯源：doc + section + quote"
    )
    rationale: str | None = Field(default=None, description="用例设计依据/假设（人读）")
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default=None, description="请求体（JSON）")
    asserts: list[Assertion] = Field(default_factory=list)
    steps: list[CaseStep] = Field(
        default_factory=list,
        description="多请求步骤；非空时按顺序执行，顶层请求/断言必须为空",
    )
    source: str = Field(description="溯源：openapi / route / requirement:<doc>#<section>")
    assert_strength: AssertStrength = Field(default=AssertStrength.L1)
    note: str | None = Field(default=None, description="生成说明/降级原因")
    mock_bindings: dict[str, str] = Field(
        default_factory=dict,
        description="用例级 Mock 分支绑定：dep_id → branch（C3.5.5 会话）",
    )


# ───────────────────────── Attempt（执行尝试）─────────────────────────

class Attempt(BaseModel):
    """单次执行尝试（C7.3 产出，C7.4 消费）。"""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(description="第几次尝试（1-based）")
    status: str = Field(description="passed/failed/error")
    reason: str | None = Field(default=None, description="失败简述")
    action: str | None = Field(default=None, description="重试动作")
    root_cause: RootCause | None = Field(default=None, description="根因类别")
    action_target: ActionTarget | None = Field(
        default=None, description="动作归属路由：user/framework"
    )


# ───────────────────────── TokenUsage（Token 统计）─────────────────────────

class TokenUsage(BaseModel):
    """Token 消耗（LLM 网关采集，C7.4 汇总，不进质量门禁）。"""

    model_config = ConfigDict(extra="forbid")

    node: str = Field(description="LLM 节点，如 A1/A4/A5")
    model: str = Field(description="模型名")
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)


# ───────────────────────── Report（报告）─────────────────────────

class Report(BaseModel):
    """运行报告（C7.4 产出，人/CI 消费）。"""

    model_config = ConfigDict(extra="forbid")

    pass_rate: float = Field(description="最终通过率")
    sut_defects: list[str] = Field(default_factory=list, description="真实缺陷清单")
    flaky_candidates: list[str] = Field(default_factory=list, description="疑似 flaky")
    token_usage: list[TokenUsage] = Field(default_factory=list, description="token 汇总")
    gate_conclusion: str = Field(description="门禁结论")
