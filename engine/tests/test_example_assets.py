"""示例工程资产与当前规则库保持一致。"""
from __future__ import annotations

import json
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "partner_onboarding"


def test_example_semantics_draft_only_references_current_rules() -> None:
    ruleset = json.loads((EXAMPLE / "ruleset.json").read_text(encoding="utf-8"))
    draft = json.loads(
        (EXAMPLE / "data_semantics.draft.json").read_text(encoding="utf-8")
    )
    known = {rule.get("rule_key") for rule in ruleset.get("rules", [])}
    referenced: set[str] = set()
    for field in draft.get("field_semantics", []):
        for value in field.get("value_semantics", []):
            referenced.update(value.get("rule_keys", []))
    for state in draft.get("states", []):
        referenced.update(state.get("rule_keys", []))
    assert referenced - known == set()
