"""跨协议 Mock runtime 的生命周期与调用采集接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from contracts import MockCall, MockSession, Transport

from .server import MockHttpServer


class MockRuntimeError(RuntimeError):
    """多 runtime 编排失败。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class MockRuntimeAdapter(ABC):
    """单个协议 Mock runtime 的统一控制接口。

    start/stop 负责进程或监听资源；activate/collect/deactivate 负责用例级
    状态。没有独立进程的 adapter 可以把 start/stop 实现为空操作。
    """

    name: str
    transport: Transport

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    @abstractmethod
    def activate(self, session: MockSession) -> None:
        """让当前用例的 binding 在该协议 runtime 中生效。"""

    @abstractmethod
    def collect(self, session: MockSession) -> list[MockCall]:
        """返回当前 session 已知的协议调用，不修改原始证据。"""

    @abstractmethod
    def deactivate(self, session: MockSession) -> None:
        """撤销当前用例状态；必须幂等。"""


class EmbeddedMockRuntimeAdapter(MockRuntimeAdapter):
    """把现有内嵌控制面包装成 HTTP 或 Dubbo 协议 adapter。

    当前 HTTP 和 Dubbo 证据都由 MockHttpServer 直接写入，所以 activate /
    deactivate 无需物理装桩；collect 只按协议筛选同一份证据。
    """

    def __init__(
        self,
        server: MockHttpServer,
        *,
        name: str,
        transport: Transport,
    ):
        if transport not in (Transport.HTTP, Transport.DUBBO):
            raise ValueError(f"unsupported embedded transport: {transport.value}")
        self.server = server
        self.name = name
        self.transport = transport

    def activate(self, session: MockSession) -> None:
        return None

    def collect(self, session: MockSession) -> list[MockCall]:
        return [
            call
            for call in self.server.evidence.records
            if call.session_id == session.session_id and self._owns(call)
        ]

    def deactivate(self, session: MockSession) -> None:
        return None

    def _owns(self, call: MockCall) -> bool:
        is_dubbo = call.adapter == "dubbo-filter" or call.mode == "agent_filter"
        return is_dubbo if self.transport is Transport.DUBBO else not is_dubbo


class MockRuntimeManager:
    """管理同一次 run 内的多协议 Mock adapter。"""

    def __init__(self, adapters: Iterable[MockRuntimeAdapter] = ()):
        self._adapters: list[MockRuntimeAdapter] = []
        self._started = False
        for adapter in adapters:
            self.register(adapter)

    @property
    def adapters(self) -> list[MockRuntimeAdapter]:
        return list(self._adapters)

    def register(self, adapter: MockRuntimeAdapter) -> None:
        if self._started:
            raise MockRuntimeError("mock_runtime_already_started", adapter.name)
        if any(item.name == adapter.name for item in self._adapters):
            raise MockRuntimeError("mock_runtime_duplicate", adapter.name)
        self._adapters.append(adapter)

    def start(self) -> None:
        if self._started:
            return
        started: list[MockRuntimeAdapter] = []
        try:
            for adapter in self._adapters:
                adapter.start()
                started.append(adapter)
        except Exception as exc:
            for adapter in reversed(started):
                try:
                    adapter.stop()
                except Exception:
                    pass
            raise MockRuntimeError(
                "mock_runtime_start_failed",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        errors: list[str] = []
        for adapter in reversed(self._adapters):
            try:
                adapter.stop()
            except Exception as exc:
                errors.append(f"{adapter.name}:{type(exc).__name__}:{exc}")
        self._started = False
        if errors:
            raise MockRuntimeError("mock_runtime_stop_failed", "; ".join(errors))

    def activate(self, session: MockSession) -> None:
        activated: list[MockRuntimeAdapter] = []
        try:
            for adapter in self._adapters:
                adapter.activate(session)
                activated.append(adapter)
        except Exception as exc:
            for adapter in reversed(activated):
                try:
                    adapter.deactivate(session)
                except Exception:
                    pass
            raise MockRuntimeError(
                "mock_runtime_activate_failed",
                f"{type(exc).__name__}: {exc}",
            ) from exc

    def collect(self, session: MockSession) -> list[MockCall]:
        calls: list[MockCall] = []
        seen: set[str] = set()
        for adapter in self._adapters:
            for call in adapter.collect(session):
                if call.session_id != session.session_id or call.call_id in seen:
                    continue
                seen.add(call.call_id)
                calls.append(call)
        return calls

    def deactivate(self, session: MockSession) -> None:
        errors: list[str] = []
        for adapter in reversed(self._adapters):
            try:
                adapter.deactivate(session)
            except Exception as exc:
                errors.append(f"{adapter.name}:{type(exc).__name__}:{exc}")
        if errors:
            raise MockRuntimeError(
                "mock_runtime_deactivate_failed",
                "; ".join(errors),
            )


__all__ = [
    "EmbeddedMockRuntimeAdapter",
    "MockRuntimeAdapter",
    "MockRuntimeError",
    "MockRuntimeManager",
]
