"""结构化约束的确定性冲突检测与人工裁决。"""
from __future__ import annotations

from pathlib import Path

from astsight.cases.draft import DraftCase
from astsight.cases.validate import ValidationResult, _check_rule_refs
from astsight.rules.draft import RuleDraft
from astsight.rules.store import import_rule_draft, load_rule_set
from contracts import RuleRef, RuleStatus

DOC = "docs/req.md"
SECTION = "订单规则"
SECTION_TEXT = (
    "订单金额必须大于 10。订单金额必须小于 5。"
    "订单量必须大于 1。订单量必须小于 5。"
    "状态只能是 `pending` 或 `paid`。"
    "状态只能是 `pending`、`paid` 或 `cancelled`。"
    "库存不足返回 400。库存不足返回 422。"
)


def _context() -> dict:
    return {
        "schema_version": 1,
        "context_id": "ctx-conflicts-1",
        "project": "demo",
        "documents": [
            {
                "doc": DOC,
                "version": "sha-1",
                "sections": [
                    {
                        "section": SECTION,
                        "path": SECTION,
                        "number": "1",
                        "level": 2,
                        "text": SECTION_TEXT,
                    }
                ],
            }
        ],
        "doc_versions": {DOC: "sha-1"},
        "operations": [],
    }


def _rule(text: str, **constraint) -> dict:
    return {
        "text": text,
        "source": {"doc": DOC, "section": SECTION, "quote": text},
        "authority": "A",
        "constraints": [constraint],
    }


def _draft(*rules: dict, conflicts: list[dict] | None = None) -> RuleDraft:
    return RuleDraft.model_validate(
        {
            "schema_version": 1,
            "context_id": "ctx-conflicts-1",
            "rules": list(rules),
            "conflicts": conflicts or [],
        }
    )


def test_range_conflict_is_recorded_without_blocking_rules(tmp_path: Path) -> None:
    context = _context()
    path = tmp_path / "ruleset.json"
    result = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(
                "订单金额必须大于 10",
                field="amount",
                operator="min",
                values=["10"],
            ),
            _rule(
                "订单金额必须小于 5",
                field="amount",
                operator="max",
                values=["5"],
            ),
        ),
        path=path,
    )
    assert result.written, result.validation.errors
    assert len(result.ruleset.conflicts) == 1
    conflict = result.ruleset.conflicts[0]
    assert conflict.origin == "framework"
    assert conflict.topic == "constraint:amount"
    assert len(conflict.rule_keys) == 2
    assert "min > max" in conflict.reason
    # 规则阶段不设人工裁决：冲突只记录，规则仍可用于 A4，留给用例审核确认。
    assert all(rule.status is RuleStatus.ACTIVE for rule in result.ruleset.rules)

    stored = load_rule_set(path)
    assert stored.conflicts[0].topic == conflict.topic
    assert all(rule.status is RuleStatus.ACTIVE for rule in stored.rules)

    # 重新导入时冲突重新计算，规则状态不受影响。
    reimported = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(
                "订单金额必须大于 10",
                field="amount",
                operator="min",
                values=["10"],
            ),
            _rule(
                "订单金额必须小于 5",
                field="amount",
                operator="max",
                values=["5"],
            ),
        ),
        previous=stored,
        path=path,
    )
    assert reimported.written, reimported.validation.errors
    assert reimported.ruleset.conflicts[0].topic == conflict.topic
    assert all(
        rule.status is RuleStatus.ACTIVE for rule in reimported.ruleset.rules
    )


def test_stale_framework_conflict_disappears_on_reimport(tmp_path: Path) -> None:
    context = _context()
    path = tmp_path / "ruleset.json"
    first = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(
                "订单金额必须大于 10",
                field="amount",
                operator="min",
                values=["10"],
            ),
            _rule(
                "订单金额必须小于 5",
                field="amount",
                operator="max",
                values=["5"],
            ),
        ),
        path=path,
    )
    assert first.ruleset.conflicts

    # 第二条规则去掉结构化约束后，框架不再检测到冲突；旧框架冲突不能残留。
    second = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(
                "订单金额必须大于 10",
                field="amount",
                operator="min",
                values=["10"],
            ),
            _rule("订单金额必须小于 5", field="", operator="other"),
        ),
        previous=first.ruleset,
        path=path,
    )
    assert second.written, second.validation.errors
    assert second.ruleset.conflicts == []
    assert all(rule.status is RuleStatus.ACTIVE for rule in second.ruleset.rules)


def test_stale_model_conflict_disappears_after_reextract(tmp_path: Path) -> None:
    """回写需求后重新抽取，上一版的模型冲突不能残留。

    每次 import 都是完整抽取。旧冲突若永久保留，修复需求后 review 文档仍会
    显示同一个缺陷，闭环无法判断是否已经收敛。
    """
    context = _context()
    path = tmp_path / "ruleset.json"
    rules = (
        _rule("订单金额必须大于 10", field="amount", operator="min", values=["10"]),
        _rule("订单量必须大于 1", field="quantity", operator="min", values=["1"]),
    )
    first = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            *rules,
            conflicts=[
                {
                    "topic": "金额与数量口径混用",
                    "kind": "semantic",
                    "severity": "loss",
                    "rule_ids": ["BR-01", "BR-02"],
                    "docs": [DOC],
                }
            ],
        ),
        path=path,
    )
    assert first.written, first.validation.errors
    assert [conflict.origin for conflict in first.ruleset.conflicts] == ["model"]

    second = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(*rules),
        previous=first.ruleset,
        path=path,
    )
    assert second.written, second.validation.errors
    assert second.ruleset.conflicts == []


def test_case_referencing_conflicting_rule_only_warns() -> None:
    imported = import_rule_draft(
        project="demo",
        context=_context(),
        draft=_draft(
            _rule(
                "订单金额必须大于 10",
                field="amount",
                operator="min",
                values=["10"],
            ),
            _rule(
                "订单金额必须小于 5",
                field="amount",
                operator="max",
                values=["5"],
            ),
        ),
    )
    rule = imported.ruleset.rules[0]
    draft_case = DraftCase(
        case_key="amount_rule_case",
        endpoint="POST /orders",
        rule_refs=[RuleRef(rule_key=rule.rule_key, rule_id=rule.rule_id)],
        asserts=[{"kind": "status_code", "op": "eq", "expect": 400}],
    )
    validation = ValidationResult(context_id="ctx-conflicts-1")
    _check_rule_refs(
        draft_case,
        ruleset=imported.ruleset,
        require_rules=True,
        case_key=draft_case.case_key,
        endpoint=draft_case.endpoint,
        path="",
        result=validation,
    )
    # 冲突不再阻断用例，但必须在审核信息里可见。
    assert not validation.errors
    assert [item.code for item in validation.warnings] == ["rule.conflict"]


def test_compatible_ranges_do_not_conflict() -> None:
    result = import_rule_draft(
        project="demo",
        context=_context(),
        draft=_draft(
            _rule(
                "订单量必须大于 1",
                field="qty",
                operator="min",
                values=["1"],
            ),
            _rule(
                "订单量必须小于 5",
                field="qty",
                operator="max",
                values=["5"],
            ),
        ),
    )
    assert not result.validation.errors
    assert result.ruleset.conflicts == []
    assert all(rule.status is RuleStatus.ACTIVE for rule in result.ruleset.rules)


def test_exhaustive_enum_sets_conflict() -> None:
    result = import_rule_draft(
        project="demo",
        context=_context(),
        draft=_draft(
            _rule(
                "状态只能是 `pending` 或 `paid`",
                field="status",
                operator="in",
                values=["pending", "paid"],
                exhaustive=True,
            ),
            _rule(
                "状态只能是 `pending`、`paid` 或 `cancelled`",
                field="status",
                operator="in",
                values=["pending", "paid", "cancelled"],
                exhaustive=True,
            ),
        ),
    )
    assert not result.validation.errors
    assert len(result.ruleset.conflicts) == 1
    conflict = result.ruleset.conflicts[0]
    assert conflict.origin == "framework"
    assert conflict.topic == "constraint:status"
    assert "完整集合" in conflict.reason


def test_error_code_conflict_for_same_condition() -> None:
    result = import_rule_draft(
        project="demo",
        context=_context(),
        draft=_draft(
            _rule(
                "库存不足返回 400",
                field="stock",
                operator="eq",
                values=[],
                condition="库存不足",
                error_code="400",
            ),
            _rule(
                "库存不足返回 422",
                field="stock",
                operator="eq",
                values=[],
                condition="库存不足",
                error_code="422",
            ),
        ),
    )
    assert not result.validation.errors
    assert len(result.ruleset.conflicts) == 1
    assert "错误码不一致" in result.ruleset.conflicts[0].reason
