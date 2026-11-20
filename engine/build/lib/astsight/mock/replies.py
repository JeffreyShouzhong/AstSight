"""协议中立分支到协议专属 Mock 响应的兼容解析。"""
from __future__ import annotations

from contracts import (
    DubboMockReply,
    GrpcMockReply,
    HttpMockReply,
    MockBranch,
    MockOutcome,
    MockReply,
    MockStub,
    Transport,
)


class MockReplyError(ValueError):
    """分支响应与目标协议不兼容。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def reply_from_legacy_stub(transport: Transport, stub: MockStub) -> MockReply:
    """把旧 HTTP-centric MockStub 转成目标协议的显式响应。

    这是迁移期兼容层。新数据应直接填写 MockBranch.reply；gRPC 非成功
    分支无法从 HTTP status 推导 canonical code，必须显式声明。
    """
    if transport is Transport.HTTP:
        return HttpMockReply(
            status=stub.status,
            headers=dict(stub.headers),
            body=stub.body,
            delay_ms=stub.delay_ms,
            drop_connection=stub.drop_connection,
        )
    if transport is Transport.DUBBO:
        return DubboMockReply(
            body=stub.body,
            delay_ms=stub.delay_ms,
            drop_connection=stub.drop_connection,
        )
    raise MockReplyError(
        "mock_reply_required",
        f"transport={transport.value} 需要显式 MockReply，不能从 HTTP status 推导",
    )


def resolve_branch_reply(
    transport: Transport,
    branch: MockBranch,
    *,
    allow_legacy: bool = True,
) -> MockReply:
    """解析当前分支的目标协议响应，并校验 transport 一致。"""
    reply = branch.reply
    if reply is not None:
        if reply.transport != transport.value:
            raise MockReplyError(
                "mock_reply_transport_mismatch",
                f"branch={branch.branch} reply={reply.transport} "
                f"target={transport.value}",
            )
        return reply
    if transport is Transport.GRPC and branch.outcome is MockOutcome.SUCCESS:
        return GrpcMockReply(
            body=branch.stub.body,
            delay_ms=branch.stub.delay_ms,
        )
    if allow_legacy:
        return reply_from_legacy_stub(transport, branch.stub)
    raise MockReplyError(
        "mock_reply_required",
        f"branch={branch.branch} transport={transport.value}",
    )


__all__ = [
    "MockReplyError",
    "reply_from_legacy_stub",
    "resolve_branch_reply",
]
