"""Mock 运行计划（C3.5.5 P0）。

`MockPlan` 是运行期输入：C3 画像或人工编写，描述一个 `sut_id` 的下游路由、
语义分支和注入策略。它不包含 admin token，token 由运行时随机生成。

计划文件属于用户配置而非证据；仍限制大小/数量，避免误读超大文件。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import MockStrategy
from .mock import MockStub


class MockPlanRoute(BaseModel):
    """一个下游端点在 Mock 服务中的路由与分支。"""

    model_config = ConfigDict(extra="forbid")

    dep_id: str = Field(description="http:{downstream_key} {METHOD} {path_key}")
    method: str
    path_key: str = Field(description="下游完整路径，占位符统一为 {}")
    host: str | None = Field(default=None, description="Host 路由使用，可带端口")
    target: str | None = Field(default=None, description="规范化目标 URL，仅作证据")
    strategy: MockStrategy | None = Field(
        default=None, description="缺省时由 injection 推导；两者都缺省按 url_override"
    )
    source: str = "plan"
    route_key: str | None = Field(
        default=None, description="可选；提供时必须等于 dep_id 的确定性 route_key"
    )
    branches: dict[str, MockStub] = Field(default_factory=dict)


class MockPlanInjection(BaseModel):
    """下游 URL 注入计划；运行时把 `injected` 补全后写入 manifest。"""

    model_config = ConfigDict(extra="forbid")

    dep_id: str
    strategy: MockStrategy
    kind: str = Field(default="base_url", description="base_url / endpoint_url / proxy")
    property: str | None = Field(default=None, description="SUT 中实际覆盖的配置键")
    original: str | None = Field(default=None, description="被覆盖前的值")
    path_mode: str = Field(default="append", description="append / replace")
    reason: str | None = Field(default=None, description="unsupported 时必填")


class MockPlan(BaseModel):
    """一个 `sut_id` 的完整 Mock 运行计划。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    sut_id: str
    namespace: str | None = Field(
        default=None, description="会话命名空间；缺省等于 sut_id"
    )
    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65_535)
    capture_bodies: bool = False
    routes: list[MockPlanRoute] = Field(default_factory=list)
    injections: list[MockPlanInjection] = Field(default_factory=list)


__all__ = ["MockPlan", "MockPlanInjection", "MockPlanRoute"]
