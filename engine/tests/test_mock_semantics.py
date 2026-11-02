"""C3.5.1 Mock 语义：规则映射分支、无依据分支拒绝、success 覆盖。"""
from __future__ import annotations

from astsight.mock.semantic import (
    apply_mock_semantic_candidates,
    build_mock_semantic,
    validate_mock_semantic,
)
from contracts import (
    Evidence,
    MockBranch,
    MockBranchSource,
    MockDep,
    MockSemantic,
    MockStub,
    Priority,
    Rule,
    RuleAuthority,
    RuleSet,
    RuleSource,
    RuleStatus,
    RuleType,
)

KEY = "RK-111111111111"
TIMEOUT_TEXT = "库存服务超时时应返回 504"


def _context() -> dict:
    return {
        "context_id": "ctx-mock-1",
        "profiles": [
            {
                "endpoint": "GET /orders/{order_id}",
                "downstream_deps": [
                    {
                        "dep_id": "inventory",
                        "method": "GET",
                        "path_key": "/stock",
                        "config_keys": ["inventory.url"],
                        "service_name": "inventory-service",
                    }
                ],
            }
        ],
    }


def _rule(
    text: str = TIMEOUT_TEXT,
    *,
    type: RuleType = RuleType.ERROR_HANDLING,
    status: RuleStatus = RuleStatus.ACTIVE,
    key: str = KEY,
) -> Rule:
    return Rule(
        rule_key=key,
        rule_id="BR-01",
        text=text,
        type=type,
        source=RuleSource(doc="docs/req.md", section="依赖"),
        authority=RuleAuthority.B,
        priority=Priority.P1,
        status=status,
    )


def _ruleset(*rules: Rule) -> RuleSet:
    return RuleSet(
        schema_version=1,
        ruleset_id="rs-mock",
        project="demo",
        version=1,
        rules=list(rules),
    )


def test_contract_layer_always_has_success_branch() -> None:
    semantic = build_mock_semantic(_context())
    assert len(semantic.deps) == 1
    dep = semantic.deps[0]
    assert dep.dep_id == "inventory"
    assert dep.endpoint == "GET /stock"
    assert [branch.branch for branch in dep.branches] == ["success"]
    assert dep.branches[0].source.kind == "contract"
    assert dep.branches[0].stub.status == 200


def test_error_rule_becomes_grounded_branch() -> None:
    semantic = build_mock_semantic(_context(), _ruleset(_rule()))
    dep = semantic.deps[0]
    branch = next(item for item in dep.branches if item.branch == "rule_BR-01")
    assert branch.stub.status == 504
    assert branch.source.kind == "rule"
    assert branch.source.rule_keys == [KEY]
    assert branch.evidence is Evidence.INFERRED
    assert not validate_mock_semantic(semantic, _context(), ruleset=_ruleset(_rule()))


def test_inactive_or_non_error_rules_do_not_create_branches() -> None:
    for ruleset in (
        _ruleset(_rule(status=RuleStatus.DEPRECATED)),
        _ruleset(_rule(status=RuleStatus.DISPUTED)),
        _ruleset(_rule(type=RuleType.CALCULATION)),
    ):
        semantic = build_mock_semantic(_context(), ruleset)
        assert [branch.branch for branch in semantic.deps[0].branches] == ["success"]


def test_candidate_branches_must_be_grounded() -> None:
    semantic = build_mock_semantic(_context(), _ruleset(_rule()))
    result = apply_mock_semantic_candidates(
        semantic,
        {
            "deps": [
                {
                    "dep_id": "inventory",
                    "branches": [
                        {
                            "branch": "degraded",
                            "semantics": "库存降级",
                            "source": {"kind": "contract", "rule_keys": []},
                        },
                        {
                            "branch": "bad_rule",
                            "semantics": "无依据状态",
                            "source": {"kind": "rule", "rule_keys": ["RK-ffffffffffff"]},
                        },
                    ],
                },
                {
                    "dep_id": "payments",
                    "branches": [],
                },
            ],
            "questions": [],
        },
        context=_context(),
        ruleset=_ruleset(_rule()),
    )
    assert "branch_ungrounded:inventory:degraded" in result.errors
    assert any(error.startswith("branch_rule_unknown:inventory:bad_rule") for error in result.errors)
    assert "dep_unknown:payments" in result.errors
    assert [branch.branch for branch in semantic.deps[0].branches] == [
        "success",
        "rule_BR-01",
    ]


def test_success_stub_override_and_evidence_downgrade() -> None:
    semantic = build_mock_semantic(_context(), _ruleset(_rule()))
    result = apply_mock_semantic_candidates(
        semantic,
        {
            "deps": [
                {
                    "dep_id": "inventory",
                    "branches": [
                        {
                            "branch": "success",
                            "semantics": "库存正常且有货",
                            "stub": {"status": 200, "body": {"stock": 5}},
                            "source": {"kind": "rule", "rule_keys": [KEY]},
                        },
                        {
                            "branch": "slow",
                            "semantics": "库存慢响应",
                            "stub": {"status": 200, "delay_ms": 500},
                            "evidence": "verified",
                            "source": {"kind": "rule", "rule_keys": [KEY]},
                        },
                    ],
                }
            ]
        },
        context=_context(),
        ruleset=_ruleset(_rule()),
    )
    assert result.errors == []
    assert "branch_stub_overridden:inventory:success" in result.warnings
    assert "branch_downgraded:inventory:slow" in result.warnings
    branches = semantic.dep_index()["inventory"].branches
    success = next(branch for branch in branches if branch.branch == "success")
    assert success.stub.body == {"stock": 5}
    slow = next(branch for branch in branches if branch.branch == "slow")
    assert slow.evidence is Evidence.INFERRED


def test_duplicate_or_invalid_branches_are_rejected() -> None:
    semantic = build_mock_semantic(_context(), _ruleset(_rule()))
    result = apply_mock_semantic_candidates(
        semantic,
        {
            "deps": [
                {
                    "dep_id": "inventory",
                    "branches": [
                        {
                            "branch": "rule_BR-01",
                            "semantics": "重复",
                            "source": {"kind": "rule", "rule_keys": [KEY]},
                        }
                    ],
                }
            ]
        },
        context=_context(),
        ruleset=_ruleset(_rule()),
    )
    assert "branch_duplicate:inventory:rule_BR-01" in result.errors


def test_validate_flags_unknown_dep_and_rule_branch_without_keys() -> None:
    semantic = MockSemantic(
        schema_version=1,
        deps=[
            MockDep(
                dep_id="payments",
                branches=[
                    MockBranch(
                        branch="rule_x",
                        semantics="异常状态",
                        stub=MockStub(status=500),
                        source=MockBranchSource(kind="rule", rule_keys=[]),
                    )
                ],
            )
        ],
    )
    errors = validate_mock_semantic(semantic, _context(), ruleset=_ruleset(_rule()))
    assert "dep_not_in_profile:payments" in errors
    assert "branch_rule_missing:payments:rule_x" in errors
