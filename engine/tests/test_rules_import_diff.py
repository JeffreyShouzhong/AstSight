"""规则上下文导入、CR 确定性提取与 diff/drift/orphaned。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from astsight.rules.context import prepare_rules_context, resolve_section
from astsight.rules.contract import build_contract_rules
from astsight.rules.diff import diff_rules, orphaned_rule_keys
from astsight.rules.draft import RuleDraft
from astsight.rules.store import import_rule_draft, load_rule_set
from astsight.rules.validate import contract_rule_key
from contracts import ContractRule, RuleAuthority, RuleSource, RuleStatus, Transport

DOC = "docs/req.md"
RULE_TEXT = "订单金额必须大于 0"
SECTION_TEXT = "订单金额必须大于 0，且同一订单最多使用 1 张优惠券。"


REQUIREMENT = f"""# 订单服务

## 订单规则

{SECTION_TEXT}
"""


def _context() -> dict:
    return {
        "schema_version": 1,
        "context_id": "ctx-diff-1",
        "project": "demo",
        "documents": [
            {
                "doc": DOC,
                "version": "sha-1",
                "sections": [
                    {
                        "section": "订单服务 > 订单规则",
                        "path": "订单服务 > 订单规则",
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


def _rule(**overrides) -> dict:
    payload = {
        "text": RULE_TEXT,
        "source": {"doc": DOC, "number": "1", "quote": RULE_TEXT},
        "authority": "B",
    }
    payload.update(overrides)
    return payload


def _draft(*rules: dict, context_id: str = "ctx-diff-1") -> RuleDraft:
    return RuleDraft.model_validate(
        {"schema_version": 1, "context_id": context_id, "rules": list(rules)}
    )


def _contract_rule() -> ContractRule:
    key = contract_rule_key("http", "POST /orders", "body.customer_id", "type=string")
    return ContractRule(
        rule_key=key,
        rule_id="CR-01",
        transport=Transport.HTTP,
        endpoint="POST /orders",
        field="body.customer_id",
        constraint="type=string",
        source=RuleSource(doc="openapi.json", section="POST /orders"),
        authority=RuleAuthority.A,
        status=RuleStatus.ACTIVE,
    )


def test_prepare_context_resolves_section_number_to_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "req.md").write_text(REQUIREMENT, encoding="utf-8")
    prepared = prepare_rules_context(project, ["docs/req.md"])

    context = prepared.context
    section = resolve_section(context, DOC, "1")
    assert section is not None
    assert section["path"] == "订单服务 > 订单规则"
    assert (prepared.context_dir / "context.json").is_file()

    result = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(_rule(), context_id=context["context_id"]),
        path=tmp_path / "ruleset.json",
    )
    assert result.written, result.validation.errors
    rule = result.ruleset.rules[0]
    # number 被规范化为稳定 path，来源仍可按原文核对。
    assert rule.source.section == "订单服务 > 订单规则"
    assert rule.source.quote == RULE_TEXT


def test_contract_rules_are_deterministic() -> None:
    operation = SimpleNamespace(
        endpoint="POST /orders",
        params=[
            SimpleNamespace(
                name="quantity",
                location="query",
                constraints={"minimum": 1},
                required=True,
                type="integer",
            )
        ],
        body_schema={
            "type": "object",
            "required": ["customer_id"],
            "properties": {
                "customer_id": {"type": "string", "minLength": 3},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}},
                    },
                },
            },
        },
    )
    first = build_contract_rules([operation])
    second = build_contract_rules([operation])
    assert [rule.model_dump() for rule in first] == [rule.model_dump() for rule in second]
    assert [rule.rule_id for rule in first] == ["CR-01", "CR-02", "CR-03", "CR-04"]
    assert {rule.field for rule in first} == {
        "body.customer_id",
        "body.items",
        "body.items[].sku",
        "query.quantity",
    }
    for rule in first:
        assert rule.rule_key == contract_rule_key(
            rule.transport.value, rule.endpoint, rule.field, rule.constraint
        )
        assert rule.authority is RuleAuthority.A


def test_diff_reports_new_updated_deprecated_and_orphans() -> None:
    context = _context()
    contract = _contract_rule()

    v1 = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(_rule(fields=["amount"])),
        contract_rules=[contract],
    )
    assert not v1.validation.errors
    assert v1.diff.as_dict()["counts"] == {
        "new": 2,
        "updated": 0,
        "deprecated": 0,
        "unchanged": 0,
    }

    v2 = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(
            _rule(fields=["amount", "currency"]),
            _rule(text="订单必须支持取消"),
        ),
        previous=v1.ruleset,
        contract_rules=[contract],
    )
    assert not v2.validation.errors
    assert v2.ruleset.version == 2
    counts = v2.diff.as_dict()["counts"]
    assert counts["new"] == 1
    assert counts["updated"] == 1
    assert counts["unchanged"] == 1
    assert len(v2.diff.drift) == 1
    assert v2.diff.drift[0].rule_key == v1.ruleset.rules[0].rule_key

    v3 = import_rule_draft(
        project="demo",
        context=context,
        draft=_draft(_rule(fields=["amount", "currency"])),
        previous=v2.ruleset,
        contract_rules=[],
    )
    assert v3.ruleset.version == 3
    assert v3.diff.as_dict()["counts"]["deprecated"] == 2
    known = set(v3.ruleset.rule_index())
    survivors = sorted(known)
    assert orphaned_rule_keys([*survivors, "RK-00000000dead"], v3.ruleset) == [
        "RK-00000000dead"
    ]

    # diff 是纯函数：同一 previous 重复计算结果一致。
    again = diff_rules(v2.ruleset, v3.ruleset.rules, v3.ruleset.contract_rules)
    assert again.as_dict() == v3.diff.as_dict()


def test_roundtrip_ruleset_hash_is_stable(tmp_path: Path) -> None:
    from astsight.rules.store import rule_set_hash

    result = import_rule_draft(
        project="demo",
        context=_context(),
        draft=_draft(_rule()),
        contract_rules=[_contract_rule()],
        path=tmp_path / "ruleset.json",
    )
    assert result.written, result.validation.errors
    loaded = load_rule_set(tmp_path / "ruleset.json")
    assert rule_set_hash(loaded) == rule_set_hash(result.ruleset)
    assert loaded.version == 1
