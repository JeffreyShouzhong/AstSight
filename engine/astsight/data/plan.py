"""C5b 单用例造数计划：catalog + approved cases → DataPlanBundle。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from contracts import (
    CaseEntry,
    DataCatalog,
    DataPlan,
    DataPlanBundle,
    Evidence,
    RejectedPlan,
    RuleSet,
    TestCase,
)

from ..cases.data_refs import resolve_case_data
from ..persistence import sha256_json
from .fixtures import prepare_plan_fixtures
from .render import RenderError, render_plan
from .validate import validate_plan_fixtures

MAX_PLANS = 1_000


class PlanError(ValueError):
    """造数计划输入不可用。"""


def _plan_id(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "dp-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _case_label(case: TestCase) -> str:
    return str(getattr(case, "case_id", None) or getattr(case, "case_key", "") or "")


def _merge_rows(
    target: dict[str, list[dict[str, Any]]],
    table: str,
    rows: Iterable[dict[str, Any]],
) -> None:
    bucket = target.setdefault(table, [])
    for row in rows:
        if row not in bucket:
            bucket.append(dict(row))


def _fixtures_from_states(
    catalog: DataCatalog,
    state_keys: Iterable[str],
    payload: dict[str, Any] | None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[str]]:
    errors: list[str] = []
    fixtures: dict[str, list[dict[str, Any]]] = {}
    target_state: dict[str, Any] = {}
    states = catalog.state_index()
    for state_key in state_keys:
        state = states.get(state_key)
        if state is None:
            errors.append(f"state_unknown:{state_key}")
            continue
        for table, rows in state.table_values.items():
            _merge_rows(fixtures, table, rows)
        if state.target_state:
            target_state.update(state.target_state)
    if payload:
        raw_fixtures = payload.get("fixtures") or {}
        if not isinstance(raw_fixtures, dict):
            errors.append("fixtures_not_object")
        else:
            for table, rows in raw_fixtures.items():
                if not isinstance(rows, list):
                    errors.append(f"fixtures_not_list:{table}")
                    continue
                _merge_rows(fixtures, str(table), rows)
        raw_target = payload.get("target_state") or {}
        if isinstance(raw_target, dict):
            target_state.update(raw_target)
        elif raw_target:
            errors.append("target_state_not_object")
    return fixtures, target_state, errors


def _check_precondition_rows(
    case: TestCase | CaseEntry,
    fixtures: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """显式 table/where 的前置状态必须能在插入行里找到。

    只检查同时给了表和条件的 precondition；只写 `state_key` 的由 catalog
    状态展开负责。用例里的 `$data` 已在上游解析成实体值。
    """
    errors: list[str] = []
    for item in getattr(case, "preconditions", []) or []:
        table = getattr(item, "table", None)
        where = getattr(item, "where", None) or {}
        if not table or not where:
            continue
        rows = fixtures.get(str(table)) or []
        if not rows:
            errors.append(f"precondition_table_missing:{table}")
            continue
        if not any(
            isinstance(row, dict)
            and all(row.get(key) == value for key, value in where.items())
            for row in rows
        ):
            errors.append(
                f"precondition_row_missing:{table}:"
                f"{json.dumps(where, ensure_ascii=False, sort_keys=True)}"
            )
    return errors


def _validate_target_state(
    catalog: DataCatalog, target_state: dict[str, Any]
) -> list[str]:
    tables = catalog.table_index()
    errors: list[str] = []
    for table_name, value in target_state.items():
        table = tables.get(str(table_name))
        if table is None:
            errors.append(f"target_table_unknown:{table_name}")
            continue
        columns = table.column_index()
        if isinstance(value, dict):
            for name in value:
                if name not in columns:
                    errors.append(f"target_column_unknown:{table_name}.{name}")
        elif isinstance(value, list):
            for row in value:
                if not isinstance(row, dict):
                    errors.append(f"target_row_not_object:{table_name}")
                    continue
                for name in row:
                    if name not in columns:
                        errors.append(f"target_column_unknown:{table_name}.{name}")
        else:
            errors.append(f"target_not_object_or_list:{table_name}")
    return errors


def _derive_post_assertions(
    case: TestCase | CaseEntry,
    catalog: DataCatalog,
    state_keys: list[str],
    fixtures: dict[str, list[dict[str, Any]]],
    target_state: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """只对实体表单列迁移生成迁移后只读断言。

    限制：正例、`transition_*` 状态、单表单列目标、目标列不是 to_/new_/after_
    成对字段、单列主键且 fixture 恰好一行。其它情况只保留 target_state 作为线索。
    """
    if str(getattr(case, "category", "")) != "positive":
        return []
    states = catalog.state_index()
    tables = catalog.table_index()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()
    for state_key in state_keys:
        state = states.get(state_key)
        if state is None or not state.state_key.startswith("transition_"):
            continue
        if len(state.target_state) != 1:
            continue
        table_name, columns = next(iter(state.target_state.items()))
        if not isinstance(columns, dict) or len(columns) != 1:
            continue
        column_name, value = next(iter(columns.items()))
        if str(column_name).lower().startswith(("to_", "new_", "after_")):
            continue
        table = tables.get(table_name)
        if table is None:
            warnings.append(
                f"target_assertion_skipped:{state_key}:table_unknown"
            )
            continue
        column = table.column_index().get(column_name)
        if column is None:
            warnings.append(
                f"target_assertion_skipped:{state_key}:column_unknown"
            )
            continue
        if len(table.primary_key) != 1:
            warnings.append(
                f"target_assertion_skipped:{state_key}:composite_primary_key"
            )
            continue
        pk_name = table.primary_key[0]
        rows = fixtures.get(table_name) or []
        if len(rows) != 1:
            warnings.append(
                f"target_assertion_skipped:{state_key}:row_not_unique"
            )
            continue
        pk_value = rows[0].get(pk_name)
        if pk_value is None:
            warnings.append(
                f"target_assertion_skipped:{state_key}:primary_key_missing"
            )
            continue
        if value is None and not column.nullable:
            warnings.append(
                f"target_assertion_skipped:{state_key}:not_null_target"
            )
            continue
        target = f"{table_name}.{column_name}"
        where = {pk_name: pk_value}
        existing = [
            assertion
            for assertion in getattr(case, "asserts", []) or []
            if str(getattr(assertion, "kind", "")) == "db_row"
            and str(getattr(assertion, "target", "")) == target
            and (getattr(assertion, "where", None) or {}) == where
        ]
        if existing:
            continue
        marker = (target, tuple(sorted(where.items())))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(
            {
                "kind": "db_row",
                "table": table_name,
                "column": column_name,
                "target": target,
                "op": "eq",
                "expect": target_state.get(table_name, {}).get(
                    column_name, value
                ),
                "where": where,
                "wait_ms": 0,
                "source": "transition_target",
            }
        )
    return out


def build_data_plan(
    *,
    case: TestCase | CaseEntry,
    catalog: DataCatalog,
    ruleset: RuleSet | None = None,
    fixtures: dict[str, list[dict[str, Any]]] | None = None,
    target_state: dict[str, Any] | None = None,
    run_scope: str = "",
) -> tuple[DataPlan | None, RejectedPlan | None, list[str]]:
    warnings: list[str] = []
    case = resolve_case_data(case)
    payload = None
    if fixtures is not None or target_state is not None:
        payload = {"fixtures": fixtures or {}, "target_state": target_state or {}}
    state_keys = [item.state_key for item in getattr(case, "preconditions", []) or []]
    if not state_keys and payload is None:
        return None, None, warnings

    merged, merged_target, errors = _fixtures_from_states(
        catalog, state_keys, payload
    )
    if errors:
        return None, RejectedPlan(
            case_id=getattr(case, "id", None),
            case_key=getattr(case, "case_key", ""),
            reason="data_plan_invalid",
            details="; ".join(errors),
        ), warnings
    if not merged:
        return None, RejectedPlan(
            case_id=getattr(case, "id", None),
            case_key=getattr(case, "case_key", ""),
            reason="fixtures_missing",
            details="前置状态/模型输出没有产生任何 fixture 行",
        ), warnings
    merged, relation_errors, relation_warnings = prepare_plan_fixtures(
        catalog,
        merged,
    )
    warnings.extend(relation_warnings)
    if relation_errors:
        return None, RejectedPlan(
            case_id=getattr(case, "id", None),
            case_key=getattr(case, "case_key", ""),
            reason="fixture_relation_invalid",
            details="; ".join(relation_errors[:20]),
        ), warnings
    merged_target.update(target_state or {})

    fixture_errors = validate_plan_fixtures(catalog, merged)
    if fixture_errors:
        return None, RejectedPlan(
            case_id=getattr(case, "id", None),
            case_key=getattr(case, "case_key", ""),
            reason="data_plan_invalid",
            details="; ".join(fixture_errors[:20]),
        ), warnings
    target_errors = _validate_target_state(catalog, merged_target)
    if target_errors:
        return None, RejectedPlan(
            case_id=getattr(case, "id", None),
            case_key=getattr(case, "case_key", ""),
            reason="target_state_invalid",
            details="; ".join(target_errors[:20]),
        ), warnings
    precondition_errors = _check_precondition_rows(case, merged)
    if precondition_errors:
        return None, RejectedPlan(
            case_id=getattr(case, "id", None),
            case_key=getattr(case, "case_key", ""),
            reason="precondition_row_missing",
            details="; ".join(precondition_errors[:20]),
        ), warnings

    tables = catalog.table_index()
    for table in merged:
        if not tables[table].primary_key:
            warnings.append(f"table_without_primary_key:{table}")
    post_assertions = _derive_post_assertions(
        case, catalog, state_keys, merged, merged_target, warnings
    )

    order = [name for name in catalog.tables_order if name in merged]
    plan_payload = {
        "catalog_id": catalog.catalog_id,
        "dialect": catalog.dialect,
        "case": _case_label(case),
        "fixtures": merged,
        "target_state": merged_target,
        "post_assertions": post_assertions,
        "run_scope": run_scope,
    }
    plan = DataPlan(
        schema_version=1,
        plan_id=_plan_id(plan_payload),
        catalog_id=catalog.catalog_id,
        dialect=catalog.dialect,
        ruleset_hash=sha256_json(ruleset.model_dump(mode="json")) if ruleset else "",
        case_id=getattr(case, "id", None),
        case_key=getattr(case, "case_key", ""),
        run_scope=run_scope,
        tables_order=order,
        fixtures=merged,
        target_state=merged_target,
        post_assertions=post_assertions,
        fk_links=[
            f"{fk.child_table}.{','.join(fk.child_columns)} -> "
            f"{fk.parent_table}.{','.join(fk.parent_columns)}"
            for fk in catalog.foreign_keys
            if fk.child_table in merged and fk.parent_table in merged
        ],
        preconditions=[
            item.model_dump(mode="json")
            for item in getattr(case, "preconditions", []) or []
        ],
        teardown="delete_by_primary_key",
        statements=[],
        warnings=[],
        evidence=[Evidence.INFERRED.value],
    )
    try:
        render_plan(plan, catalog)
    except RenderError as exc:
        return None, RejectedPlan(
            case_id=getattr(case, "id", None),
            case_key=getattr(case, "case_key", ""),
            reason="data_plan_render_failed",
            details=str(exc),
        ), warnings
    plan.warnings = list(dict.fromkeys([*plan.warnings, *warnings]))
    return plan, None, warnings


def build_data_plan_bundle(
    *,
    cases: Iterable[TestCase | CaseEntry],
    catalog: DataCatalog,
    ruleset: RuleSet | None = None,
    payloads: dict[str, dict[str, Any]] | None = None,
    run_scope: str = "",
) -> DataPlanBundle:
    case_list = list(cases)
    if len(case_list) > MAX_PLANS:
        raise PlanError(f"用例数 {len(case_list)} 超过上限 {MAX_PLANS}")
    scope = run_scope or "run-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    payloads = payloads or {}
    bundle = DataPlanBundle(
        schema_version=1,
        run_scope=scope,
        plans=[],
        rejected=[],
        warnings=[],
    )
    for case in case_list:
        key = str(getattr(case, "case_key", "") or getattr(case, "id", ""))
        payload = payloads.get(key) or payloads.get(str(getattr(case, "id", "") or ""))
        plan, rejected, warnings = build_data_plan(
            case=case,
            catalog=catalog,
            ruleset=ruleset,
            fixtures=(payload or {}).get("fixtures"),
            target_state=(payload or {}).get("target_state"),
            run_scope=scope,
        )
        bundle.warnings.extend(item for item in warnings if item not in bundle.warnings)
        if plan is not None:
            bundle.plans.append(plan)
        if rejected is not None:
            bundle.rejected.append(rejected)
    return bundle


__all__ = ["MAX_PLANS", "PlanError", "build_data_plan", "build_data_plan_bundle"]
