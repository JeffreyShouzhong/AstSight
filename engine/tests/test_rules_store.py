"""规则库：rule_key/fingerprint 稳定、A→B 降级、冲突裁决、失败不落盘。"""
from __future__ import annotations

from pathlib import Path

import pytest

from astsight.rules.draft import RuleDraft
from astsight.rules.store import (
    RuleStoreError,
    import_rule_draft,
    load_rule_set,
    resolve_conflict,
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

    changed = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(_rule(text="订单金额必须大于 0.01")),
    ).ruleset.rules[0]
    assert changed.rule_key != rule.rule_key
    assert changed.fingerprint != rule.fingerprint


def test_ungrounded_a_level_rules_are_downgraded() -> None:
    context = _context()
    result = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(text="订单金额必须大于 100"),
            _rule(
                text="支付状态枚举为 `refunded`",
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
    assert by_text["订单金额必须大于 100"].authority is RuleAuthority.B
    assert by_text["支付状态枚举为 `refunded`"].authority is RuleAuthority.B
    ungrounded = by_text["订单必须支持取消"]
    assert ungrounded.authority is RuleAuthority.B
    assert ungrounded.status is RuleStatus.UNGROUNDED


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


def test_conflict_keeps_rules_disputed_until_resolved(tmp_path: Path) -> None:
    context = _context()
    path = tmp_path / "ruleset.json"
    imported = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(rule_id="BR-01", text="订单金额必须大于 0"),
            _rule(rule_id="BR-02", text="同一订单最多使用 1 张优惠券"),
            conflicts=[{"topic": "下单门槛", "rule_ids": ["BR-01", "BR-02"]}],
        ),
        path=path,
    )
    assert imported.written, imported.validation.errors
    assert imported.validation.errors == []
    assert [rule.status for rule in imported.ruleset.rules] == [
        RuleStatus.DISPUTED,
        RuleStatus.DISPUTED,
    ]
    stored = load_rule_set(path)
    assert stored.rules[0].status is RuleStatus.DISPUTED

    chosen = imported.ruleset.rules[0].rule_key
    other = imported.ruleset.rules[1].rule_key
    resolved = resolve_conflict(
        ruleset=imported.ruleset,
        topic="下单门槛",
        choose=chosen,
        reviewer="alice",
        decision="以订单金额规则为准",
        path=path,
    )
    statuses = {rule.rule_key: rule.status for rule in resolved.rules}
    assert statuses[chosen] is RuleStatus.ACTIVE
    assert statuses[other] is RuleStatus.DEPRECATED
    conflict = resolved.conflicts[0]
    assert conflict.resolved_by == "alice"
    assert conflict.resolved_at
    # 保存后可重新加载，hash 与内存对象一致。
    reloaded = load_rule_set(path)
    assert rule_set_hash(reloaded) == rule_set_hash(resolved)
    assert rule_set_snapshot(reloaded)["hash"] == rule_set_hash(reloaded)


def test_save_rejects_tampered_rule_set(tmp_path: Path) -> None:
    result = import_rule_draft(
        project="demo", context=_context(), draft=_draft(_rule())
    )
    ruleset = result.ruleset
    ruleset.rules[0].rule_key = "RK-000000000000"
    with pytest.raises(RuleStoreError):
        save_rule_set(tmp_path / "ruleset.json", ruleset)
