"""协议中立分支与协议专属 Mock 响应的契约测试。"""
from __future__ import annotations

import pytest

from astsight.mock import MockReplyError, resolve_branch_reply
from contracts import (
    DubboMockReply,
    GrpcMockReply,
    GrpcStatus,
    HttpMockReply,
    MockBranch,
    MockBranchSource,
    MockOutcome,
    MockStub,
    Transport,
)


def _branch(**overrides) -> MockBranch:
    payload = {
        "branch": "success",
        "semantics": "下游成功",
        "stub": MockStub(body={"ok": True}),
        "source": MockBranchSource(kind="contract"),
    }
    payload.update(overrides)
    return MockBranch(**payload)


def test_protocol_replies_use_discriminated_union() -> None:
    http = MockBranch(
        branch="success",
        semantics="HTTP success",
        reply={
            "transport": "http",
            "status": 201,
            "body": {"id": "O-1"},
        },
        source=MockBranchSource(kind="contract"),
    )
    grpc = MockBranch(
        branch="not_found",
        semantics="gRPC not found",
        outcome=MockOutcome.NOT_FOUND,
        reply={
            "transport": "grpc",
            "grpc_status": "NOT_FOUND",
            "message": "missing",
        },
        source=MockBranchSource(kind="contract"),
    )

    assert isinstance(http.reply, HttpMockReply)
    assert http.reply.status == 201
    assert isinstance(grpc.reply, GrpcMockReply)
    assert grpc.reply.grpc_status is GrpcStatus.NOT_FOUND


def test_legacy_http_stub_maps_to_dubbo_reply() -> None:
    branch = _branch(
        stub=MockStub(
            status=500,
            headers={"x-http-only": "yes"},
            body={"orderId": "O-1"},
            delay_ms=25,
            drop_connection=True,
        )
    )

    reply = resolve_branch_reply(Transport.DUBBO, branch)

    assert isinstance(reply, DubboMockReply)
    assert reply.body == {"orderId": "O-1"}
    assert reply.delay_ms == 25
    assert reply.drop_connection is True


def test_legacy_grpc_success_maps_but_error_requires_explicit_reply() -> None:
    success = resolve_branch_reply(Transport.GRPC, _branch())
    assert isinstance(success, GrpcMockReply)
    assert success.grpc_status is GrpcStatus.OK

    failed = _branch(outcome=MockOutcome.NOT_FOUND)
    with pytest.raises(MockReplyError) as error:
        resolve_branch_reply(Transport.GRPC, failed)
    assert error.value.code == "mock_reply_required"


def test_reply_transport_must_match_dependency() -> None:
    branch = _branch(
        reply={
            "transport": "grpc",
            "grpc_status": "OK",
        }
    )
    with pytest.raises(MockReplyError) as error:
        resolve_branch_reply(Transport.HTTP, branch)
    assert error.value.code == "mock_reply_transport_mismatch"
