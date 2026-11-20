"""CLI data 命令的 service 实现。"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from codegraph import (
    CODEGRAPH_VERSION,
    install as install_codegraph_runtime,
    status as codegraph_runtime_status,
)
from contracts import DataCatalog, DataPlanBundle, MockPlan, RuleSet
from ..cases.context import load_context, prepare_context
from ..cases.draft import load_draft
from ..cases.proposal import (
    ProposalError,
    approve_proposal,
    build_proposal,
    classify,
    interactive_review,
    list_pending_proposals,
)
from ..cases.store import (
    case_summary,
    empty_case_set,
    load_case_set,
    requirement_coverage,
    resolve_case_base,
)
from ..cases.validate import validate_draft
from ..data import (
    SeedData,
    apply_bundle,
    apply_semantics,
    build_catalog,
    build_data_plan_bundle,
    load_seed,
    parse_dbml,
    parse_ddl,
    reflect_database,
    validate_catalog,
    validate_plan_fixtures,
)
from ..data.semantics import SemanticDraft
from ..inventory import discover_openapi, inventory_routes
from ..mock import MockPlanError, validate_mock_plan
from ..mock.semantic import (
    apply_mock_semantic_candidates,
    build_mock_semantic,
    validate_mock_semantic,
)
from ..persistence import load_json, save_json_atomic
from ..feedback import collect_feedback, open_issue
from ..pipeline import RunConfig, run
from ..profile import build_plan_from_profiles, build_profiles, load_spec, parse_operations
from ..rules import (
    RuleDraft,
    RuleStoreError,
    build_rules_context,
    import_rule_draft,
    load_rule_set,
    load_rules_context,
    load_rule_draft,
    prepare_rules_context,
    render_rule_set_markdown,
    rule_coverage,
    rule_set_hash,
    rule_set_snapshot,
    save_rule_set,
    write_rules_context,
)

from ._shared import (
    _load_optional_ruleset,
    _resolve_under_project,
)


def _data_path(project: Path, raw: str | None, default_name: str) -> Path:
    path = Path(raw) if raw else project / ".astsight" / "data" / default_name
    if not path.is_absolute():
        path = project / path
    if path.suffix.lower() != ".json":
        path = path / default_name
    return path.resolve()


def _data_catalog(args) -> int:
    project = Path(args.project).resolve()
    try:
        if args.db_url:
            dialect = _catalog_dialect(args.db_url, args.dialect)
            schemas = [
                item.strip()
                for item in str(args.schema or "").split(",")
                if item.strip()
            ]
            reflection = reflect_database(
                args.db_url,
                schemas=schemas,
                include=args.include,
                exclude=args.exclude,
            )
            parsed = reflection.ddl
            sources = reflection.sources
        elif args.dbml:
            _catalog_reject_reflect_only(args)
            dialect = args.dialect or "sqlite"
            path = Path(_resolve_under_project(project, args.dbml) or args.dbml)
            parsed = parse_dbml(path.read_text(encoding="utf-8-sig"))
            sources = {
                "kind": "dbml",
                "file": _project_relative(project, path),
            }
        else:
            _catalog_reject_reflect_only(args)
            dialect = args.dialect or "sqlite"
            text = "\n;\n".join(
                Path(_resolve_under_project(project, raw) or raw).read_text(
                    encoding="utf-8-sig"
                )
                for raw in args.ddl
            )
            parsed = parse_ddl(text, dialect=dialect)
            sources = {"kind": "ddl", "files": list(args.ddl)}
        seed = load_seed(_resolve_under_project(project, args.seed), parsed)
        context_id = None
        if args.context:
            context = load_rules_context(args.context)
            context_id = str(context.get("context_id") or "")
        ruleset = _load_optional_ruleset(project, args.ruleset)
        if args.ruleset and ruleset is None:
            raise ValueError(f"规则库不存在: {args.ruleset}")
        if args.rule_states and ruleset is None:
            raise ValueError("--rule-states 需要 --ruleset")
        scan = None
        if args.code:
            from ..data import scan_code_enums

            scan = scan_code_enums(project, args.code)
        catalog = build_catalog(
            project=project.name,
            ddl=parsed,
            seed=seed,
            context_id=context_id,
            dialect=dialect,
            sources=sources,
            ruleset=ruleset,
            code_enums=list(scan.enums) if scan else None,
            rule_states=args.rule_states,
        )
        if scan and scan.warnings:
            catalog.warnings.extend(scan.warnings)
        errors = validate_catalog(catalog)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        out = _data_path(project, args.out, "data_catalog.json")
        save_json_atomic(out, catalog.model_dump(mode="json"))
    except Exception as exc:
        print(f"data catalog 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "catalog": str(out),
                "catalog_id": catalog.catalog_id,
                "source": sources.get("kind"),
                "dialect": dialect,
                "schemas": sources.get("schemas") or [],
                "tables": len(catalog.tables),
                "foreign_keys": len(catalog.foreign_keys),
                "tables_order": catalog.tables_order,
                "semantic_values": sum(
                    len(column.value_semantics)
                    for table in catalog.tables
                    for column in table.columns
                ),
                "code_enums": len(scan.enums) if scan else 0,
                "code_files": len(scan.files) if scan else 0,
                "rule_states": int(
                    (catalog.sources.get("rule_states") or {}).get(
                        "proposed", 0
                    )
                ),
                "gaps": len(catalog.gaps),
                "warnings": catalog.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _catalog_dialect(db_url: str, requested: str | None) -> str:
    from ..data import dialect_from_url

    inferred = dialect_from_url(db_url)
    if requested and requested != inferred:
        raise ValueError(
            f"--dialect {requested} 与 --db-url 推断的 {inferred} 不一致"
        )
    return inferred


def _catalog_reject_reflect_only(args) -> None:
    if args.schema or args.include or args.exclude:
        raise ValueError("--schema/--include/--exclude 仅用于 --db-url")


def _project_relative(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return path.name


def _data_semantics(args) -> int:
    project = Path(args.project).resolve()
    if args.draft and args.llm:
        print("--draft 与 --llm 只能二选一", file=sys.stderr)
        return 1
    if not args.draft and not args.llm:
        print(
            "需要 --draft（框架外模型输出）或 --llm（框架内调用模型）",
            file=sys.stderr,
        )
        return 1
    try:
        catalog = DataCatalog.model_validate(load_json(args.catalog))
        ruleset = _load_optional_ruleset(project, args.ruleset)
        if args.llm:
            from ..agent.data_prep import generate_data_semantics
            from ..llm import LLMClient, load_config

            context = None
            if args.context:
                context_path = _resolve_under_project(project, args.context)
                context = load_json(context_path)
            _draft, result = generate_data_semantics(
                catalog,
                llm=LLMClient(load_config(config_path=args.llm_config)),
                ruleset=ruleset,
                context=context if isinstance(context, dict) else None,
                prompt_path=args.prompt,
            )
            catalog = result.catalog
        else:
            draft_path = _resolve_under_project(project, args.draft)
            draft = SemanticDraft.model_validate(load_json(draft_path))
            result = apply_semantics(catalog, draft, ruleset=ruleset)
        if not result.ok:
            print(
                json.dumps(
                    {"ok": False, "errors": result.errors, "warnings": result.warnings},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        out = _data_path(project, args.out, "data_catalog.json")
        save_json_atomic(out, catalog.model_dump(mode="json"))
    except Exception as exc:
        print(f"data semantics 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "catalog": str(out),
                "fields": result.applied_fields,
                "states": result.applied_states,
                "warnings": result.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _data_states(args) -> int:
    project = Path(args.project).resolve()
    try:
        catalog = DataCatalog.model_validate(load_json(args.catalog))
        errors = validate_catalog(catalog)
        if errors:
            print(
                json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2)
            )
            return 1
        states = catalog.states
        if args.state:
            states = [
                state for state in states if state.state_key == args.state
            ]
            if not states:
                print(f"state 不存在: {args.state}", file=sys.stderr)
                return 1
        gaps_by_state: dict[str, list[dict[str, Any]]] = {}
        for gap in catalog.gaps:
            key = str(gap.get("state_key") or "")
            if key:
                gaps_by_state.setdefault(key, []).append(gap)
        payload: list[dict[str, Any]] = []
        for state in states:
            payload.append(
                {
                    "state_key": state.state_key,
                    "rule_keys": list(state.rule_keys),
                    "tables": {
                        table: len(rows)
                        for table, rows in state.table_values.items()
                    },
                    "target_state": state.target_state,
                    "gaps": gaps_by_state.get(state.state_key, []),
                    "errors": validate_plan_fixtures(
                        catalog, state.table_values
                    ),
                    "evidence": state.evidence.value,
                }
            )
        summary = {
            "states": len(catalog.states),
            "paths": len(catalog.state_paths),
            "shown": len(payload),
            "rule_states": sum(
                1
                for state in catalog.states
                if state.state_key.startswith("rule_")
            ),
            "transition_states": sum(
                1
                for state in catalog.states
                if state.state_key.startswith("transition_")
            ),
            "seed_states": sum(
                1
                for state in catalog.states
                if state.state_key.startswith("seed_")
            ),
        }
    except Exception as exc:
        print(f"data states 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "summary": summary,
                    "states": payload,
                    "paths": [
                        path.model_dump(mode="json")
                        for path in catalog.state_paths
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        f"states: {summary['states']} "
        f"(rule={summary['rule_states']}, "
        f"transition={summary['transition_states']}, "
        f"seed={summary['seed_states']}); shown={summary['shown']}; "
        f"paths={summary['paths']}"
    )
    for item in payload:
        tables = ", ".join(
            f"{table}:{rows}" for table, rows in item["tables"].items()
        )
        target = (
            json.dumps(item["target_state"], ensure_ascii=False)
            if item["target_state"]
            else "-"
        )
        print(f"- {item['state_key']}  tables={tables}  target={target}")
        for gap in item["gaps"]:
            column = ".".join(
                part
                for part in (gap.get("table"), gap.get("column"))
                if part
            )
            print(
                f"    gap {gap.get('kind')}: {column} — {gap.get('reason', '')}"
            )
        for error in item["errors"]:
            print(f"    invalid: {error}")
    if catalog.state_paths:
        print(f"paths: {len(catalog.state_paths)}")
        for path in catalog.state_paths:
            print(f"- {path.path_key}  table={path.table}  column={path.column}")
            values = [str(path.steps[0].from_value)] + [
                str(step.to_value) for step in path.steps
            ]
            print("    " + " → ".join(values))
            for step in path.steps:
                print(
                    f"    {step.from_value} → {step.to_value}"
                    f"  ({step.rule_key}, {step.condition})"
                )
    return 0


def _data_plan(args) -> int:
    project = Path(args.project).resolve()
    try:
        catalog = DataCatalog.model_validate(load_json(args.catalog))
        errors = validate_catalog(catalog)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        case_base = _resolve_under_project(project, args.cases) or project / "caseset.json"
        caseset = load_case_set(case_base)
        if caseset is None:
            print(f"用例库不存在: {case_base}", file=sys.stderr)
            return 1
        from ..cases.store import approved_cases

        ruleset = _load_optional_ruleset(project, args.ruleset)
        payloads: dict[str, Any] = {}
        if args.fixtures:
            raw = load_json(args.fixtures)
            if isinstance(raw, dict) and "plans" in raw:
                payloads = dict(raw.get("plans") or {})
            elif isinstance(raw, dict):
                payloads = dict(raw)
        bundle = build_data_plan_bundle(
            cases=approved_cases(caseset),
            catalog=catalog,
            ruleset=ruleset,
            payloads=payloads,
            run_scope=args.run_scope or "",
        )
        out = _data_path(project, args.out, "data_plan.json")
        save_json_atomic(out, bundle.model_dump(mode="json"))
    except Exception as exc:
        print(f"data plan 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": not bundle.rejected,
                "plan": str(out),
                "run_scope": bundle.run_scope,
                "plans": len(bundle.plans),
                "post_assertions": sum(
                    len(plan.post_assertions) for plan in bundle.plans
                ),
                "rejected": [
                    item.model_dump(mode="json") for item in bundle.rejected
                ],
                "warnings": bundle.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if bundle.rejected else 0


def _data_apply(args) -> int:
    try:
        data = load_json(args.plan)
        if isinstance(data, dict) and "plans" in data:
            bundle = DataPlanBundle.model_validate(data)
        else:
            bundle = DataPlanBundle.model_validate(
                {"schema_version": 1, "run_scope": "", "plans": [data]}
            )
        result = apply_bundle(
            db_url=args.db_url,
            bundle=bundle,
            run_scope=args.run_scope,
            keep=args.keep_data,
        )
    except Exception as exc:
        print(f"data apply 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0
