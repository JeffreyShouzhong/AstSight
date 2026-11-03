"""规则库：rule_key/fingerprint 稳定、A→B 降级、冲突记录、失败不落盘。"""
from __future__ import annotations

from pathlib import Path

import pytest

from astsight.rules.draft import RuleDraft
from astsight.rules.store import (
    RuleStoreError,
    import_rule_draft,
    load_rule_set,
    rule_set_hash,
    rule_set_snapshot,
    save_rule_set,
)
from astsight.rules.validate import rule_fingerprint, rule_key
from contracts import RuleAuthority, RuleStatus

DOC = "docs/req.md"
SECTION = "订单规则"
SECTION_TEXT = (
    "订单金额必须大于 0，且同一订单最多使用 1 张优惠券。"
    "支付状态枚举为 `paid`。"
)


def _context() -> dict:
    return {
        "schema_version": 1,
        "context_id": "ctx-rules-1",
        "project": "demo",
        "documents": [
            {
                "doc": DOC,
                "version": "sha-req-1",
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
        "doc_versions": {DOC: "sha-req-1"},
        "operations": [],
    }


def _rule(**overrides) -> dict:
    payload = {
        "text": "订单金额必须大于 0",
        "source": {"doc": DOC, "section": SECTION, "quote": "订单金额必须大于 0"},
        "authority": "A",
    }
    payload.update(overrides)
    return payload


def _draft(*rules: dict, conflicts: list[dict] | None = None) -> RuleDraft:
    return RuleDraft.model_validate(
        {
            "schema_version": 1,
            "context_id": "ctx-rules-1",
            "rules": list(rules),
            "conflicts": conflicts or [],
        }
    )


def test_rule_key_and_fingerprint_are_derived_not_trusted() -> None:
    context = _context()
    text = "订单金额必须大于 0"
    result = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(_rule(rule_id="BR-99", reasoning="模型自述")),
    )
    assert not result.validation.errors
    rule = result.ruleset.rules[0]
    assert rule.rule_key == rule_key(DOC, SECTION, text)
    assert rule.fingerprint == rule_fingerprint(rule)
    # 展示编号保留，reasoning 不进 fingerprint。
    assert rule.rule_id == "BR-99"

    again = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(_rule(rule_id="BR-01", reasoning="另一段解释")),
    ).ruleset.rules[0]
    assert again.rule_key == rule.rule_key
    assert again.fingerprint == rule.fingerprint

    second_text = "同一订单最多使用 1 张优惠券"
    changed = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(
                text=second_text,
                source={"doc": DOC, "section": SECTION, "quote": second_text},
            )
        ),
    ).ruleset.rules[0]
    assert changed.rule_key != rule.rule_key
    assert changed.fingerprint != rule.fingerprint


def test_ungrounded_a_level_rules_are_downgraded() -> None:
    context = _context()
    result = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(text="订单金额必须大于 1"),
            _rule(
                text="支付状态枚举为 `paid`",
                source={"doc": DOC, "section": SECTION, "quote": "支付状态枚举为"},
            ),
            _rule(
                text="订单必须支持取消",
                source={"doc": DOC, "section": SECTION, "quote": ""},
            ),
        ),
    )
    assert not result.validation.errors
    assert any("降为 B" in warning for warning in result.validation.warnings)
    by_text = {rule.text: rule for rule in result.ruleset.rules}
    assert by_text["订单金额必须大于 1"].authority is RuleAuthority.B
    assert by_text["订单金额必须大于 1"].status is RuleStatus.UNGROUNDED
    assert by_text["支付状态枚举为 `paid`"].authority is RuleAuthority.B
    assert by_text["支付状态枚举为 `paid`"].status is RuleStatus.UNGROUNDED
    ungrounded = by_text["订单必须支持取消"]
    assert ungrounded.authority is RuleAuthority.B
    assert ungrounded.status is RuleStatus.UNGROUNDED


def test_fabricated_values_are_rejected() -> None:
    context = _context()
    result = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(_rule(text="订单金额必须大于 100")),
    )
    assert result.validation.errors
    assert any("疑似编造" in error for error in result.validation.errors)
    assert result.ruleset.rules == []


def test_structured_constraint_values_must_be_grounded() -> None:
    context = _context()
    result = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(
                text="订单金额必须大于 0",
                constraints=[
                    {
                        "field": "amount",
                        "operator": "min",
                        "values": ["0"],
                    }
                ],
            )
        ),
    )
    assert not result.validation.errors
    assert result.ruleset.rules[0].constraints[0].values == ["0"]

    fabricated = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(
                text="订单金额必须大于 0",
                constraints=[
                    {
                        "field": "amount",
                        "operator": "min",
                        "values": ["100"],
                    }
                ],
            )
        ),
    )
    assert fabricated.validation.errors
    assert any("疑似编造" in error for error in fabricated.validation.errors)


def test_quote_must_exist_in_section_and_failure_keeps_ruleset_bytes(
    tmp_path: Path,
) -> None:
    context = _context()
    path = tmp_path / "ruleset.json"
    first = import_rule_draft(
        project="demo", context=context, draft=_draft(_rule()), path=path
    )
    assert first.written
    before = path.read_bytes()
    previous = load_rule_set(path)

    bad = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(source={"doc": DOC, "section": SECTION, "quote": "订单金额必须小于 0"})
        ),
        previous=previous,
        path=path,
    )
    assert bad.validation.errors
    assert not bad.written
    assert path.read_bytes() == before


def test_conflict_is_recorded_without_changing_rule_status(tmp_path: Path) -> None:
    context = _context()
    path = tmp_path / "ruleset.json"
    imported = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(rule_id="BR-01", text="订单金额必须大于 0"),
            _rule(
                rule_id="BR-02",
                text="同一订单最多使用 1 张优惠券",
                source={
                    "doc": DOC,
                    "section": SECTION,
                    "quote": "同一订单最多使用 1 张优惠券",
                },
            ),
            conflicts=[{"topic": "下单门槛", "rule_ids": ["BR-01", "BR-02"]}],
        ),
        path=path,
    )
    assert imported.written, imported.validation.errors
    assert imported.validation.errors == []
    # 规则阶段没有人工裁决：冲突只记录，规则保持 active，留给用例审核确认。
    assert [rule.status for rule in imported.ruleset.rules] == [
        RuleStatus.ACTIVE,
        RuleStatus.ACTIVE,
    ]
    stored = load_rule_set(path)
    assert stored.rules[0].status is RuleStatus.ACTIVE
    conflict = stored.conflicts[0]
    assert conflict.topic == "下单门槛"
    assert conflict.origin == "model"
    assert set(conflict.rule_keys) == {
        rule.rule_key for rule in imported.ruleset.rules
    }
    # 保存后可重新加载，hash 与内存对象一致。
    assert rule_set_hash(stored) == rule_set_hash(imported.ruleset)
    assert rule_set_snapshot(stored)["hash"] == rule_set_hash(stored)


def test_save_rejects_tampered_rule_set(tmp_path: Path) -> None:
    result = import_rule_draft(
        project="demo", context=_context(), draft=_draft(_rule())
    )
    ruleset = result.ruleset
    ruleset.rules[0].rule_key = "RK-000000000000"
    with pytest.raises(RuleStoreError):
        save_rule_set(tmp_path / "ruleset.json", ruleset)
