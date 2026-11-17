"""把规则关系确定性物化为用例断言。

同响应/请求关系直接转换；跨步骤关系（``$step``）先按用例的
``relation_bindings`` 把语义别名绑定到具体步骤名，再生成断言侧 ``$ref``。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from contracts import Assertion, RuleRelation, RuleSet

from .draft import CaseDraft, DraftCase
from ..rules.relations import relation_step_aliases, to_assertion_expression

MAX_ASSERTS_PER_CASE = 50
MAX_ASSERTS_PER_STEP = 50
MAX_BINDING_ALIASES = 20


@dataclass
class RelationMaterialization:
    draft: CaseDraft
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    covered_relation_keys: set[str] = field(default_factory=set)


def materialize_rule_relations(
    draft: CaseDraft,
    ruleset: RuleSet | None,
) -> RelationMaterialization:
    updated = draft.model_copy(deep=True)
    outcome = RelationMaterialization(draft=updated)
    for case in updated.cases:
        refs = set(case.relation_refs)
        for key in case.relation_bindings or {}:
            if key not in refs:
                outcome.errors.append(
                    f"{case.case_key}: relation_bindings 中的 {key} "
                    "未在 relation_refs 中选择"
                )
    if ruleset is None:
        return outcome
    index: dict[str, tuple[str, RuleRelation]] = {}
    for rule in ruleset.rules:
        for relation in rule.relations:
            if relation.relation_key:
                index[relation.relation_key] = (rule.rule_key, relation)

    for case in updated.cases:
        refs = list(dict.fromkeys(case.relation_refs))
        bindings = case.relation_bindings or {}
        if not refs:
            continue
        rule_keys = {ref.rule_key for ref in case.rule_refs}
        for key in refs:
            entry = index.get(key)
            if entry is None:
                outcome.errors.append(
                    f"{case.case_key}: relation_ref 不存在: {key}"
                )
                continue
            owner_rule_key, relation = entry
            if owner_rule_key not in rule_keys:
                outcome.errors.append(
                    f"{case.case_key}: relation_ref {key} 属于未引用的规则 "
                    f"{owner_rule_key}"
                )
                continue
            if _has_materialized(case, key):
                outcome.covered_relation_keys.add(key)
                continue
            aliases = relation_step_aliases(relation)
            step_map: dict[str, str] = {}
            if aliases:
                resolved = _resolve_bindings(
                    case,
                    key,
                    aliases,
                    bindings.get(key) or {},
                    outcome,
                )
                if resolved is None:
                    continue
                step_map = resolved
            elif key in bindings:
                outcome.errors.append(
                    f"{case.case_key}: 关系 {key} 不含 $step，不需要 relation_bindings"
                )
                continue
            assertion = _build_assertion(relation, step_map)
            target = case.steps[-1] if case.steps else case
            limit = (
                MAX_ASSERTS_PER_STEP if case.steps else MAX_ASSERTS_PER_CASE
            )
            if len(target.asserts) >= limit:
                outcome.errors.append(
                    f"{case.case_key}: 关系 {key} 使断言数超过上限 {limit}"
                )
                continue
            target.asserts.append(assertion)
            outcome.covered_relation_keys.add(key)
    return outcome


def _resolve_bindings(
    case: DraftCase,
    relation_key: str,
    aliases: list[str],
    binding: dict[str, str],
    outcome: RelationMaterialization,
) -> dict[str, str] | None:
    """把 ``$step`` 别名映射到用例步骤名；任何不一致都返回 None。"""
    if not case.steps:
        outcome.errors.append(
            f"{case.case_key}: 关系 {relation_key} 引用 $step，必须放在多步骤用例中"
        )
        return None
    if len(binding) > MAX_BINDING_ALIASES:
        outcome.errors.append(
            f"{case.case_key}: 关系 {relation_key} 的 relation_bindings 超过上限 "
            f"{MAX_BINDING_ALIASES}"
        )
        return None
    ok = True
    unknown = sorted(set(binding) - set(aliases))
    if unknown:
        outcome.errors.append(
            f"{case.case_key}: 关系 {relation_key} 的 relation_bindings 含未声明的"
            f"别名 {unknown}"
        )
        ok = False
    # 断言落在最后一步，$ref 只能读取更早的步骤。
    earlier = [step.name for step in case.steps[:-1]]
    all_names = [step.name for step in case.steps]
    step_map: dict[str, str] = {}
    for alias in aliases:
        name = binding.get(alias)
        if not name:
            outcome.errors.append(
                f"{case.case_key}: 关系 {relation_key} 缺少别名 {alias!r} 的步骤绑定"
            )
            ok = False
            continue
        if name not in earlier:
            if name in all_names:
                outcome.errors.append(
                    f"{case.case_key}: 关系 {relation_key} 的别名 {alias!r} 绑定到"
                    f"步骤 {name!r}，但它不早于承载断言的最后一步"
                )
            else:
                outcome.errors.append(
                    f"{case.case_key}: 关系 {relation_key} 的别名 {alias!r} 绑定了"
                    f"不存在的步骤 {name!r}"
                )
            ok = False
            continue
        step_map[alias] = name
    return step_map if ok else None


def _has_materialized(case: DraftCase, relation_key: str) -> bool:
    assertions = [
        *case.asserts,
        *(assertion for step in case.steps for assertion in step.asserts),
    ]
    return any(item.relation_ref == relation_key for item in assertions)


def _build_assertion(
    relation: RuleRelation,
    step_map: dict[str, str],
) -> Assertion:
    expect = to_assertion_expression(relation.expect, step_map=step_map)
    where = {
        key: to_assertion_expression(value, step_map=step_map)
        for key, value in relation.where.items()
    }
    return Assertion(
        kind=relation.assertion_kind,
        target=relation.target,
        op=relation.op,
        expect=expect,
        where=where,
        wait_ms=relation.wait_ms,
        relation_ref=relation.relation_key,
    )


__all__ = [
    "RelationMaterialization",
    "materialize_rule_relations",
]
