"""跨协议 Mock runtime adapter 的编排测试。"""
from __future__ import annotations

from pathlib import Path

from astsight.mock import (
    EmbeddedMockRuntimeAdapter,
    MockHttpServer,
    MockRuntimeManager,
)
from contracts import (
    MockBinding,
    MockCall,
    MockSession,
    MockStub,
    Transport,
)


def _session() -> MockSession:
    return MockSession(
        session_id="S-1",
        namespace="sut",
        case_id="TC-1",
        sut_id="sut",
        bindings=[
            MockBinding(dep_id="http:svc:user GET /users/{}", branch="success"),
            MockBinding(dep_id="dubbo:OrderService#create()", branch="success"),
        ],
        created_at="2026-11-18T00:00:00+00:00",
        expires_at="2026-11-18T00:01:00+00:00",
    )


def _call(call_id: str, *, adapter: str | None = None) -> MockCall:
    return MockCall(
        call_id=call_id,
        session_id="S-1",
        case_id="TC-1",
        dep_id=(
            "dubbo:OrderService#create()"
            if adapter == "dubbo-filter"
            else "http:svc:user GET /users/{}"
        ),
        method="GET",
        original_path="/users/1",
        adapter=adapter,
        mode="agent_filter" if adapter == "dubbo-filter" else None,
        at="2026-11-18T00:00:01+00:00",
    )


def test_embedded_adapters_collect_their_protocol_and_manager_deduplicates(
    tmp_path: Path,
) -> None:
    server = MockHttpServer(
        sut_id="sut",
        history_path=tmp_path / "mock_calls.jsonl",
    )
    http_call = _call("http-1")
    dubbo_call = _call("dubbo-1", adapter="dubbo-filter")
    server.evidence.write(http_call)
    server.evidence.write(dubbo_call)

    manager = MockRuntimeManager(
        [
            EmbeddedMockRuntimeAdapter(
                server,
                name="http_mock",
                transport=Transport.HTTP,
            ),
            EmbeddedMockRuntimeAdapter(
                server,
                name="dubbo_filter",
                transport=Transport.DUBBO,
            ),
        ]
    )
    session = _session()

    manager.activate(session)
    calls = manager.collect(session)
    manager.deactivate(session)

    assert [call.call_id for call in calls] == ["http-1", "dubbo-1"]
    assert {call.call_id for call in calls} == {"http-1", "dubbo-1"}


def test_legacy_mock_stub_constructor_still_works() -> None:
    stub = MockStub(status=202, body={"accepted": True})
    assert stub.status == 202
    assert stub.body == {"accepted": True}
