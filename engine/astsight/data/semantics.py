"""LLM 语义补全的确定性合并与校验（C5a 适配层边界）。

模型只能补 `semantic_type/value_semantics/business_notes` 和候选 `DataState`；
表/列/枚举值必须已存在于目录/DDL。无规则/需求依据的 `verified` 一律降为 inferred。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts import (
    ColumnSemantic,
    DataCatalog,
    DataState,
    Evidence,
    FieldSemantics,
    RuleSet,
    ValueSemantics,
)

from .validate import validate_plan_fixtures


class SemanticDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    field_semantics: list[FieldSemantics] = Field(default_factory=list, max_length=5_000)
    states: list[DataState] = Field(default_factory=list, max_length=500)
    gaps: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    questions: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


@dataclass
class SemanticApplyResult:
    catalog: DataCatalog
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied_fields: int = 0
    applied_states: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _known_rule_keys(ruleset: RuleSet | None) -> set[str]:
    return set(ruleset.rule_index()) if ruleset is not None else set()


def _check_values(
    table: str,
    column_name: str,
    column: Any,
    values: list[ValueSemantics],
    known_rules: set[str],
    *,
    require_enum: bool,
) -> list[str]:
    errors: list[str] = []
    for index, item in enumerate(values):
        if item.value is None:
            errors.append(f"value_semantics_null:{table}.{column_name}[{index}]")
        if require_enum and column.enum_values and item.value not in column.enum_values:
            errors.append(
                f"value_semantics_enum:{table}.{column_name}:{item.value!r}"
            )
        for key in item.rule_keys:
            if known_rules and key not in known_rules:
                errors.append(
                    f"value_semantics_rule_unknown:{table}.{column_name}:{key}"
                )
    return errors


def apply_semantics(
    catalog: DataCatalog,
    draft: SemanticDraft,
    *,
    ruleset: RuleSet | None = None,
) -> SemanticApplyResult:
    tables = catalog.table_index()
    known_rules = _known_rule_keys(ruleset)
    errors: list[str] = []
    warnings: list[str] = []
    field_index: dict[tuple[str, str], FieldSemantics] = {
        (item.table, item.column): item for item in catalog.field_semantics
    }
    applied_fields = 0

    for item in draft.field_semantics:
        table = tables.get(item.table)
        if table is None:
            errors.append(f"semantics_table_unknown:{item.table}")
            continue
        column = table.column_index().get(item.column)
        if column is None:
            errors.append(f"semantics_column_unknown:{item.table}.{item.column}")
            continue
        if not isinstance(item.semantic_type, ColumnSemantic):
            errors.append(
                f"semantics_type_invalid:{item.table}.{item.column}:{item.semantic_type}"
            )
            continue
        item_errors = _check_values(
            item.table,
            item.column,
            column,
            item.value_semantics,
            known_rules,
            require_enum=True,
        )
        errors.extend(item_errors)
        if item_errors:
            continue
        if item.evidence is Evidence.VERIFIED:
            grounded = any(value.rule_keys for value in item.value_semantics)
            if not grounded or (known_rules and any(
                key not in known_rules
                for value in item.value_semantics
                for key in value.rule_keys
            )):
                warnings.append(
                    f"semantics_downgraded:{item.table}.{item.column}"
                )
                item = item.model_copy(update={"evidence": Evidence.INFERRED})
        column.semantic_type = item.semantic_type
        column.value_semantics = [value.model_copy() for value in item.value_semantics]
        if item.evidence is Evidence.VERIFIED:
            column.evidence = Evidence.VERIFIED
        field_index[(item.table, item.column)] = item
        applied_fields += 1

    known_states = {state.state_key for state in catalog.states}
    applied_states = 0
    for state in draft.states:
        if not state.state_key:
            errors.append("state_key_empty")
            continue
        if state.state_key in known_states:
            errors.append(f"state_duplicate:{state.state_key}")
            continue
        fixture_errors = validate_plan_fixtures(catalog, state.table_values)
        if fixture_errors:
            errors.extend(
                f"state_invalid:{state.state_key}:{message}"
                for message in fixture_errors
            )
            continue
        for key in state.rule_keys:
            if known_rules and key not in known_rules:
                errors.append(f"state_rule_unknown:{state.state_key}:{key}")
        if state.evidence is Evidence.VERIFIED and not state.rule_keys:
            warnings.append(f"state_downgraded:{state.state_key}")
            state = state.model_copy(update={"evidence": Evidence.INFERRED})
        catalog.states.append(state)
        known_states.add(state.state_key)
        applied_states += 1

    for gap in draft.gaps:
        if isinstance(gap, dict):
            catalog.gaps.append(dict(gap))
    for question in draft.questions:
        if isinstance(question, dict):
            catalog.questions.append(dict(question))

    catalog.field_semantics = sorted(
        field_index.values(), key=lambda item: (item.table, item.column)
    )
    return SemanticApplyResult(
        catalog=catalog,
        errors=errors,
        warnings=warnings,
        applied_fields=applied_fields,
        applied_states=applied_states,
    )


__all__ = ["SemanticApplyResult", "SemanticDraft", "apply_semantics"]
