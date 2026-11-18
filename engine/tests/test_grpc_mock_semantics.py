"""gRPC MockSemantic 默认成功体与规则分支测试。"""
from __future__ import annotations

from astsight.mock.semantic import build_mock_semantic, validate_mock_semantic
from contracts import (
    GrpcMockReply,
    GrpcStatus,
    MockStrategy,
    Priority,
    Rule,
    RuleAuthority,
    RuleSet,
    RuleSource,
    RuleStatus,
    RuleType,
    Transport,
)

DEP_ID = "grpc:/inventory.v1.InventoryService/Reserve"
RULE_KEY = "RK-grpc111111111"


def _context() -> dict:
    return {
        "profiles": [
            {
                "endpoint": "GET /orders",
                "downstream_deps": [
                    {
                        "dep_id": DEP_ID,
                        "dep_type": "grpc",
                        "grpc_path": "/inventory.v1.InventoryService/Reserve",
                        "service": "inventory.v1.InventoryService",
                        "method": "Reserve",
                        "property": "INVENTORY_GRPC_ADDR",
                        "descriptor_source": "inventory.proto",
                        "response_schema": {
                            "available": False,
                            "reservation_id": "",
                        },
                    }
                ],
            }
        ]
    }


def _ruleset() -> RuleSet:
    return RuleSet(
        schema_version=2,
        ruleset_id="grpc-rules",
        project="demo",
        version=1,
        rules=[
            Rule(
                rule_key=RULE_KEY,
                rule_id="BR-GRPC-1",
                text="InventoryService 不可用时返回 504",
                type=RuleType.ERROR_HANDLING,
                source=RuleSource(doc="req.md", section="依赖"),
                authority=RuleAuthority.B,
                priority=Priority.P1,
                status=RuleStatus.ACTIVE,
            )
        ],
    )


def test_grpc_success_uses_descriptor_schema_and_rule_maps_to_grpc_status() -> None:
    context = _context()
    semantic = build_mock_semantic(context, _ruleset())

    validate_mock_semantic(semantic, context, ruleset=_ruleset())

    dep = semantic.deps[0]
    assert dep.transport is Transport.GRPC
    assert dep.strategy is MockStrategy.GRPC_SERVER
    success = next(item for item in dep.branches if item.branch == "success")
    assert isinstance(success.reply, GrpcMockReply)
    assert success.reply.body == {
        "available": False,
        "reservation_id": "",
    }
    rule_branch = next(
        item for item in dep.branches if item.branch == "rule_BR-GRPC-1"
    )
    assert isinstance(rule_branch.reply, GrpcMockReply)
    assert rule_branch.reply.grpc_status is GrpcStatus.UNAVAILABLE
    assert rule_branch.reply.body == {}
