"""HTTP Mock 契约（C3.5.5，v4.6.1）。

这些模型只描述可持久化/可报告的数据形态；运行期路由表、会话锁等实现细节
留在 `astsight.mock`，不进入契约层。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    MockCallResult,
    MockMode,
    MockRouteMatch,
    MockStrategy,
    MockTransport,
)


class MockStub(BaseModel):
    """一个语义分支的桩响应。

    `body` 支持 JSON 值、字符串，或 `{"raw_base64": "..."}`。
    逐跳头与 Content-Length 由服务端计算，禁止桩自带。
    """

    model_config = ConfigDict(extra="forbid")

    # 不模拟 1xx：ASGI/FastAPI 不能把 1xx 当作最终响应发送。
    status: int = Field(default=200, ge=200, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default=None)
    delay_ms: int = Field(default=0, ge=0, le=60_000)
    drop_connection: bool = Field(default=False)


class MockBinding(BaseModel):
    """会话内的下游分支绑定。"""

    model_config = ConfigDict(extra="forbid")

    dep_id: str
    branch: str


class MockRoute(BaseModel):
    """一个下游端点在 Mock 服务里的路由身份。"""

    model_config = ConfigDict(extra="forbid")

    route_key: str = Field(description="r + sha256(dep_id)[:12]，URL 前缀路由用")
    dep_id: str = Field(description="http:{downstream_key} {METHOD} {path_key}")
    method: str
    path_key: str = Field(description="归一化后的下游完整路径，占位符为 {}")
    host: str | None = Field(default=None, description="Host 路由使用的主机（可带端口）")
    target: str | None = Field(default=None, description="归一化目标 URL，仅作证据/哈希")
    strategy: MockStrategy = MockStrategy.URL_OVERRIDE
    source: str = Field(default="profile", description="profile/manual/...")
    route_match: MockRouteMatch | None = Field(
        default=None, description="最近一次命中的路由依据"
    )


class MockInjection(BaseModel):
    """URL 注入证据。"""

    model_config = ConfigDict(extra="forbid")

    dep_id: str | None = Field(default=None, description="所属下游；旧数据可为空")
    strategy: MockStrategy | None = Field(default=None)
    property: str | None = Field(default=None, description="SUT 中被覆盖的配置键")
    kind: str = Field(description="base_url / endpoint_url / proxy")
    original: str = Field(default="")
    injected: str = Field(default="")
    path_mode: str = Field(default="append", description="append / replace")
    reason: str | None = Field(default=None, description="unsupported 的显式原因")


class MockCall(BaseModel):
    """`mock_calls.jsonl` 的一行。"""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    session_id: str | None = None
    case_id: str | None = None
    route_key: str | None = None
    dep_id: str | None = None
    route_match: MockRouteMatch | None = None
    method: str
    original_path: str
    query_keys: list[str] = Field(default_factory=list)
    request_headers: dict[str, str] = Field(default_factory=dict)
    body_sha256: str = ""
    body_bytes: int = 0
    request_body: Any | None = Field(
        default=None, description="仅在 --capture-mock-bodies 时留存"
    )
    branch: str | None = None
    response_status: int | None = None
    delay_ms: int = 0
    drop_mode: str | None = Field(default=None, description="reset / timeout")
    result: MockCallResult = MockCallResult.OK
    error: str | None = None
    at: str


class MockSession(BaseModel):
    """用例级 Mock 会话。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    namespace: str
    case_id: str
    sut_id: str
    run_scope: str = ""
    bindings: list[MockBinding] = Field(default_factory=list)
    ttl_ms: int = Field(default=60_000, ge=1, le=3_600_000)
    created_at: str
    expires_at: str
    active: bool = True
    closed_at: str | None = None
    close_reason: str | None = None


class MockDrainResult(BaseModel):
    """`POST /_admin/sessions/{id}/drain` 的返回形态。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    drained: bool
    inflight: int = Field(ge=0)
    waited_ms: int = Field(ge=0)


class MockServerHealth(BaseModel):
    """`GET /_admin/health` 的返回形态。"""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    sut_id: str
    namespace: str
    route_count: int
    active_session_id: str | None = None
    inflight: int = Field(default=0, ge=0)
    uptime_ms: int = 0


class MockServer(BaseModel):
    """Mock 服务实例（C3.5.4/C3.5.5 共用形态）。"""

    model_config = ConfigDict(extra="forbid")

    transport: MockTransport
    mode: MockMode
    version: str = "v4.6.1"
    sut_id: str
    namespace: str
    endpoint: str
    admin_endpoint: str
    token_fingerprint: str
    route_modes: list[MockRouteMatch] = Field(default_factory=list)
    route_count: int = 0
    routes: list[MockRoute] = Field(default_factory=list)
    history_path: str | None = None
    health: MockServerHealth | None = None
    started_at: str | None = None
    pid: int | None = None
    injections: list[MockInjection] = Field(default_factory=list)
