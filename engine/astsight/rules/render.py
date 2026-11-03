"""规则库的 Markdown 渲染。

渲染是**只读视图**：所有内容都从 `RuleSet` 派生，不调用模型、不重新判断。
这样人工评审看到的东西与框架消费的规则库永远一致；版式要改也不必重跑抽取。

设计上刻意不让模型同时输出 JSON 和 Markdown 两份：两份由两次生成产生，
条目数、编号、数字都会对不上，而且没人能一眼看出哪份是错的。
"""
from __future__ import annotations

import re
from typing import Iterable

from contracts import (
    ConflictKind,
    ConflictSeverity,
    Rule,
    RuleConflict,
    RuleGap,
    RuleOperator,
    RuleQuestion,
    RuleSet,
)

_SEVERITY_LABEL = {
    ConflictSeverity.LOSS: "资损",
    ConflictSeverity.SILENT: "静默失效",
    ConflictSeverity.RIGHTS: "权益受损",
    ConflictSeverity.UNTESTABLE: "不可测",
    ConflictSeverity.MINOR: "表述瑕疵",
}

_KIND_LABEL = {
    ConflictKind.SEMANTIC: "词义冲突",
    ConflictKind.CROSS_SECTION: "两处口径不一致",
    ConflictKind.VALUE: "取值/阈值矛盾",
    ConflictKind.ENUMERATION_CLOSED: "枚举未闭合",
    ConflictKind.UNREACHABLE: "分支不可达",
    ConflictKind.BIDIRECTIONAL: "效应方向相反",
    ConflictKind.SELF_TRIGGER: "自反触发",
    ConflictKind.OTHER: "其他",
}

_GAP_KIND_LABEL = {
    "implicit": "隐含假设",
    "missing": "该写未写",
    "quantitative": "缺数量级",
    "cross_system": "跨系统一致性",
    "tech_choice": "技术选型未定",
}

_STATUS_LABEL = {
    "active": "有效",
    "ungrounded": "无原文落地",
    "deprecated": "已废弃",
}

_EMPTY = "—"


def _cell(value: object, limit: int = 0) -> str:
    """表格单元格：转义竖线、压平换行，必要时截断。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("|", "\\|")
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or _EMPTY


def _rule_order(rule: Rule) -> tuple[int, str]:
    match = re.match(r"^BR-(\d+)$", rule.rule_id or "")
    if match:
        return (int(match.group(1)), rule.rule_id)
    return (10**6, rule.rule_id or rule.rule_key)


def _table(headers: list[str], rows: Iterable[list[str]], empty_text: str = "") -> list[str]:
    rows = list(rows)
    if not rows:
        if empty_text:
            return [empty_text]
        return [
            "| " + " | ".join(headers) + " |",
            "|" + "|".join(["---"] * len(headers)) + "|",
            "| " + " | ".join([_EMPTY] * len(headers)) + " |",
        ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _rule_refs(keys: Iterable[str], label: dict[str, str]) -> str:
    refs = [label.get(key, key) for key in keys]
    return _cell("、".join(dict.fromkeys(refs)) if refs else "")


def _closed_enum_problems(rules: Iterable[Rule]) -> list[tuple[str, str, list[str]]]:
    """列出所有 in/not_in 但未声明 exhaustive 的约束。"""
    found: list[tuple[str, str, list[str]]] = []
    for rule in rules:
        for constraint in rule.constraints:
            if constraint.operator not in {RuleOperator.IN, RuleOperator.NOT_IN}:
                continue
            if constraint.exhaustive is not None:
                continue
            found.append(
                (
                    rule.rule_id or rule.rule_key,
                    constraint.field or "-",
                    list(constraint.values),
                )
            )
    return found


def _conflict_rows(
    conflicts: Iterable[RuleConflict], label: dict[str, str]
) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, conflict in enumerate(conflicts, start=1):
        rows.append(
            [
                f"CF-{index:02d}",
                _KIND_LABEL.get(conflict.kind, conflict.kind.value),
                _SEVERITY_LABEL.get(conflict.severity, "") if conflict.severity else _EMPTY,
                _cell(conflict.topic, 160),
                _cell(conflict.reason, 200),
                _rule_refs(conflict.rule_keys, label),
            ]
        )
    return rows


def _gap_rows(gaps: Iterable[RuleGap], label: dict[str, str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, gap in enumerate(gaps, start=1):
        rows.append(
            [
                f"GAP-{index:02d}",
                _GAP_KIND_LABEL.get(gap.kind, gap.kind),
                gap.check_dimension.value if gap.check_dimension else _EMPTY,
                _cell(gap.desc, 160),
                _cell(gap.reason, 200),
                _rule_refs(gap.affected_rule_keys, label),
            ]
        )
    return rows


def _assumption_rows(gaps: Iterable[RuleGap], label: dict[str, str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, gap in enumerate(gaps, start=1):
        rows.append(
            [
                f"AS-{index:02d}",
                _cell(gap.desc, 160),
                _cell(gap.reason, 200),
                _rule_refs(gap.affected_rule_keys, label),
            ]
        )
    return rows


def _question_rows(
    questions: Iterable[RuleQuestion], label: dict[str, str]
) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, question in enumerate(questions, start=1):
        rows.append(
            [
                question.id or f"Q-{index:02d}",
                _cell(question.text, 200),
                _cell(question.reason, 160),
                _cell(question.impact, 160),
                _cell(question.source, 80),
                _rule_refs(question.related_rule_keys, label),
            ]
        )
    return rows


def render_rule_set_markdown(ruleset: RuleSet, *, title: str | None = None) -> str:
    """把规则库渲染成一份人工评审用的 Markdown。"""
    rules = sorted(ruleset.rules, key=_rule_order)
    label = {
        rule.rule_key: rule.rule_id or rule.rule_key for rule in ruleset.all_rules()
    }

    gaps = list(ruleset.gaps)
    assumptions = [gap for gap in gaps if gap.kind == "implicit"]
    missing = [gap for gap in gaps if gap.kind != "implicit"]

    a_level = sum(1 for rule in rules if rule.authority.value == "A")
    b_level = len(rules) - a_level
    a_ratio = f"{a_level}（{a_level / len(rules):.1%}）" if rules else "0"
    ungrounded = sum(1 for rule in rules if rule.status.value == "ungrounded")
    severity_count = {
        severity: sum(1 for item in ruleset.conflicts if item.severity == severity)
        for severity in ConflictSeverity
    }

    lines: list[str] = []
    lines.append(f"# {title or ruleset.project or '规则抽取结果'}")
    lines.append("")
    lines.append(
        f"> 由 `ruleset.json` 渲染：版本 v{ruleset.version}，生成于 {ruleset.generated_at or _EMPTY}。"
    )
    lines.append(
        "> 本文件是只读视图。规则有误请回写需求文档后重新抽取，不要直接编辑规则库——"
        "手改会让规则库变成第二个人工维护的真相源。"
    )
    lines.append("")

    lines.append("## 1 统计")
    lines.append("")
    stat_rows = [
        ["业务规则", str(len(rules))],
        ["A 级（有出处、可判定）", a_ratio],
        ["B 级（需推断或缺值）", str(b_level)],
        ["无原文落地（ungrounded）", str(ungrounded)],
        ["契约规则（CR）", str(len(ruleset.contract_rules))],
        ["冲突", str(len(ruleset.conflicts))],
        ["其中资损", str(severity_count.get(ConflictSeverity.LOSS, 0))],
        ["其中静默失效", str(severity_count.get(ConflictSeverity.SILENT, 0))],
        ["定义缺口", str(len(missing))],
        ["隐含假设", str(len(assumptions))],
        ["待确认问题", str(len(ruleset.questions))],
    ]
    lines.extend(_table(["项", "数量"], stat_rows))
    lines.append("")

    lines.append("## 2 规则表")
    lines.append("")
    lines.extend(
        _table(
            ["编号", "规则", "级", "类型", "状态", "出处"],
            (
                [
                    rule.rule_id or _EMPTY,
                    _cell(rule.text, 200),
                    rule.authority.value,
                    rule.type.value,
                    _STATUS_LABEL.get(rule.status.value, rule.status.value),
                    _cell(f"{rule.source.doc}#{rule.source.section}", 80),
                ]
                for rule in rules
            ),
        )
    )
    lines.append("")

    lines.append("## 3 冲突（文档内部可判定，不需业务确认）")
    lines.append("")
    if ruleset.conflicts:
        lines.extend(
            _table(
                ["编号", "类型", "后果", "冲突", "说明", "涉及规则"],
                _conflict_rows(ruleset.conflicts, label),
            )
        )
        lines.append("")
        lines.append(
            "> 这一节列的是缺陷，不是问题。处置是改文档，不是等业务方回答。"
            "`分支不可达` 的处置是删除该段描述。"
        )
    else:
        lines.append("未检出。若文档已有语义级矛盾却在这里为空，说明抽取没覆盖到，不是文档没问题。")
    lines.append("")

    lines.append("## 4 定义缺口（文档该写未写）")
    lines.append("")
    lines.extend(
        _table(
            ["编号", "类型", "维度", "缺口", "原因", "影响规则"],
            _gap_rows(missing, label),
            empty_text="未检出。文档本身简略时这里是正常的；文档很详细却为空，建议复核覆盖查漏。",
        )
    )
    lines.append("")

    lines.append("## 5 隐含假设（原文没写但规则默认成立）")
    lines.append("")
    lines.extend(
        _table(
            ["编号", "假设", "不成立时的影响", "涉及规则"],
            _assumption_rows(assumptions, label),
            empty_text="未检出。为空不代表没有默认前提，只代表这次没识别出来。",
        )
    )
    lines.append("")

    lines.append("## 6 业务确认清单")
    lines.append("")
    lines.extend(
        _table(
            ["编号", "问题", "为什么必须问", "不回答的后果", "出处", "阻塞规则"],
            _question_rows(ruleset.questions, label),
            empty_text="无需业务确认的问题。",
        )
    )
    lines.append("")

    open_enums = _closed_enum_problems(rules)
    lines.append("## 7 枚举未闭合项（`in/not_in` 未声明 exhaustive）")
    lines.append("")
    if open_enums:
        lines.extend(
            _table(
                ["规则", "字段", "当前枚举"],
                ([rule_label, field, _cell("、".join(values), 120)] for rule_label, field, values in open_enums),
            )
        )
        lines.append("")
        lines.append(
            "> 这些枚举框架无法判断是否完整。若实际取值多于此处所列，"
            "依赖它的计算公式和状态判断都会漏掉分支。"
        )
    else:
        lines.append("无。所有 `in/not_in` 约束都显式声明了 `exhaustive`。")
    lines.append("")

    return "\n".join(lines)


__all__ = ["render_rule_set_markdown"]
