"""规则库 Markdown 渲染：只读视图，内容与 ruleset 一致。"""
from __future__ import annotations

from astsight.rules.render import render_rule_set_markdown
from contracts import (
    CheckDimension,
    ConflictKind,
    ConflictSeverity,
    Priority,
    Rule,
    RuleAuthority,
    RuleConflict,
    RuleConstraint,
    RuleGap,
    RuleOperator,
    RuleQuestion,
    RuleSet,
    RuleSource,
    RuleStatus,
    RuleType,
)


def _rule(rule_id: str, text: str, **extra) -> Rule:
    return Rule(
        rule_key=f"RK-{rule_id}",
        rule_id=rule_id,
        text=text,
        source=RuleSource(doc="docs/req.md", section="3.2 库存", quote=text),
        **extra,
    )


def _ruleset() -> RuleSet:
    return RuleSet(
        project="demo",
        version=3,
        generated_at="2026-11-03T13:00:00+00:00",
        rules=[
            _rule(
                "BR-02",
                "库存不足时返回 400 与错误码 40001",
                authority=RuleAuthority.A,
                priority=Priority.P0,
                type=RuleType.ERROR_HANDLING,
                constraints=[
                    RuleConstraint(
                        field="stock",
                        operator=RuleOperator.IN,
                        values=["0"],
                        condition="库存不足",
                    )
                ],
            ),
            _rule(
                "BR-01",
                "券退回后有效期不变 | 不得重置",
                authority=RuleAuthority.B,
                status=RuleStatus.UNGROUNDED,
                type=RuleType.STATE_TRANSITION,
            ),
        ],
        conflicts=[
            RuleConflict(
                topic="券退回后是否重置有效期",
                kind=ConflictKind.SEMANTIC,
                severity=ConflictSeverity.LOSS,
                rule_keys=["RK-BR-01"],
                origin="model",
                reason="退回的语义是恢复原状",
            )
        ],
        gaps=[
            RuleGap(
                kind="implicit",
                desc="详情页底部一定有购买栏",
                reason="原文未写",
                affected_rule_keys=["RK-BR-02"],
            ),
            RuleGap(
                kind="missing",
                desc="提现失败态未定义",
                reason="枚举只给了三种",
                check_dimension=CheckDimension.STATE_TRANSITION,
            ),
        ],
        questions=[
            RuleQuestion(
                id="Q-01",
                text="可提现为负时告警用截断前还是截断后？",
                reason="文档两处口径不一致",
                impact="告警可能永不触发",
                source="docs/req.md#3.3",
                related_rule_keys=["RK-BR-02"],
            )
        ],
    )


def test_render_contains_all_sections() -> None:
    text = render_rule_set_markdown(_ruleset())
    for heading in (
        "## 1 统计",
        "## 2 规则表",
        "## 3 冲突",
        "## 4 定义缺口",
        "## 5 隐含假设",
        "## 6 业务确认清单",
        "## 7 枚举未闭合项",
    ):
        assert heading in text


def test_render_maps_rule_key_to_rule_id() -> None:
    text = render_rule_set_markdown(_ruleset())
    assert "BR-01" in text
    assert "RK-BR-01" not in text.split("## 2")[1]


def test_render_escapes_pipe_in_cell() -> None:
    text = render_rule_set_markdown(_ruleset())
    assert r"不得重置" in text
    assert "有效期不变 \\| 不得重置" in text


def test_render_sorting_and_labels() -> None:
    text = render_rule_set_markdown(_ruleset())
    body = text.split("## 2 规则表")[1]
    assert body.index("BR-01") < body.index("BR-02")
    assert "词义冲突" in text
    assert "资损" in text
    assert "隐含假设" in text


def test_render_flags_open_enum() -> None:
    text = render_rule_set_markdown(_ruleset())
    section = text.split("## 7 枚举未闭合项")[1]
    assert "BR-02" in section
    assert "stock" in section


def test_render_empty_ruleset_is_safe() -> None:
    text = render_rule_set_markdown(RuleSet(project="empty"))
    assert "## 2 规则表" in text
    assert "未检出" in text
    assert "所有 `in/not_in` 约束都显式声明了 `exhaustive`" in text
