"""CLI mock 命令的 service 实现。"""
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


def _sut_id(raw: str) -> str:
    value = "".join("-" if ch.isspace() else ch for ch in str(raw or "")).strip("-")
    return value or "sut"


def _mock_plan_command(args) -> int:
    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"项目目录不存在: {project}", file=sys.stderr)
        return 1

    notes: list[str] = []
    openapi_source = discover_openapi(project, args.openapi, None)
    operations = []
    if openapi_source:
        try:
            operations = parse_operations(load_spec(openapi_source))
        except Exception as exc:
            notes.append(
                f"openapi_unreadable:{type(exc).__name__}:{str(exc)[:200]}"
            )
    else:
        notes.append("openapi_missing:profile_contract_incomplete")

    try:
        routes, _audit, client, graph_notes = inventory_routes(
            project,
            codegraph=args.codegraph,
            codegraph_module_root=args.codegraph_module_root,
            full_index=True if args.full_index else None,
            openapi_source=openapi_source,
        )
    except Exception as exc:
        print(f"路由清单失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not routes:
        print("未发现任何路由，无法生成 Mock 计划（不猜测）", file=sys.stderr)
        return 1

    profile_routes = [
        {
            "method": route.get("method", ""),
            "path": route.get("path", ""),
            "handler": route.get("handler", ""),
            "owner": route.get("handler_owner", ""),
            "file": route.get("file", ""),
            "source": route.get("source", ""),
            "confidence": route.get("confidence", "inferred"),
        }
        for route in routes
    ]
    try:
        profiles, _unmapped = build_profiles(
            profile_routes,
            operations,
            codegraph_client=client,
            project_root=project,
        )
    except Exception as exc:
        print(f"下游识别失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = build_plan_from_profiles(
        profiles,
        sut_id=_sut_id(args.sut_id or project.name),
        host=args.host,
        port=args.port,
    )
    if not payload["routes"]:
        print(
            "未识别到任何可静态确定的 HTTP 下游调用点；不生成空计划。"
            "请检查 handler 是否可解析、是否使用了未支持的客户端写法。",
            file=sys.stderr,
        )
        return 1

    try:
        model = MockPlan.model_validate(payload)
        validation = validate_mock_plan(model)
    except (MockPlanError, ValueError) as exc:
        print(f"生成的计划未通过校验: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else Path(".astsight/mock-plan.json")
    if not out_path.is_absolute():
        out_path = project / out_path
    out_path = out_path.resolve()
    if out_path.exists() and not args.force:
        print(
            f"输出已存在，拒绝覆盖（确认可覆盖后加 --force）: {out_path}",
            file=sys.stderr,
        )
        return 1
    try:
        save_json_atomic(out_path, model.model_dump(mode="json"))
    except OSError as exc:
        print(f"写入计划失败: {exc}", file=sys.stderr)
        return 1

    warnings: list[str] = [*notes, *graph_notes, *validation.warnings]
    unsupported_calls: dict[str, dict] = {}
    call_site_count = 0
    for profile in profiles:
        warnings.extend(profile.get("downstream_warnings") or [])
        call_site_count += len(profile.get("downstream_call_sites") or [])
        for item in profile.get("downstream_unsupported") or []:
            unsupported_calls.setdefault(item.get("call_site_id", ""), item)
    warnings = list(dict.fromkeys(item for item in warnings if item))

    unsupported_injections = sum(
        1 for item in model.injections if item.strategy.value == "unsupported"
    )
    result = {
        "ok": True,
        "plan": str(out_path),
        "sut_id": model.sut_id,
        "routes": len(model.routes),
        "injections": len(model.injections),
        "unsupported_injections": unsupported_injections,
        "downstream_call_sites": call_site_count,
        "downstream_unsupported": list(unsupported_calls.values()),
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Mock 计划草案: {result['plan']}")
        print(
            f"  sut_id: {result['sut_id']}  路由: {result['routes']}  "
            f"注入: {result['injections']}（unsupported {unsupported_injections}）"
        )
        print(
            f"  下游调用点: {call_site_count}  "
            f"未识别: {len(unsupported_calls)}"
        )
        for warning in warnings:
            print(f"  [WARN] {warning}")
        print(
            "  注意：分支留空，注入原值未知的项标 unsupported；"
            "请人工补全 branches / original 后再用 --mock-plan 执行。"
        )
    return 0


def _mock_semantics_command(args) -> int:
    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"项目目录不存在: {project}", file=sys.stderr)
        return 1
    if args.draft and args.llm:
        print("--draft 与 --llm 只能二选一", file=sys.stderr)
        return 1

    warnings: list[str] = []
    try:
        context_path = _resolve_under_project(project, args.context)
        context = load_context(context_path)
        ruleset = _load_optional_ruleset(project, args.ruleset)
        semantic = build_mock_semantic(context, ruleset)

        if args.draft:
            draft_path = _resolve_under_project(project, args.draft)
            applied = apply_mock_semantic_candidates(
                semantic,
                load_json(draft_path),
                context=context,
                ruleset=ruleset,
            )
            warnings.extend(applied.warnings)
            if not applied.ok:
                print(
                    json.dumps(
                        {"ok": False, "errors": applied.errors, "warnings": applied.warnings},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 1
            semantic = applied.semantic

        if args.llm:
            from ..agent.mock_semantics import generate_mock_semantic_candidates
            from ..llm import LLMClient, load_config

            applied = generate_mock_semantic_candidates(
                context,
                semantic,
                ruleset=ruleset,
                llm=LLMClient(load_config(config_path=args.llm_config)),
                prompt_path=args.prompt,
            )
            if applied is not None:
                warnings.extend(applied.warnings)
                if not applied.ok:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "errors": applied.errors,
                                "warnings": applied.warnings,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 1
                semantic = applied.semantic

        errors = validate_mock_semantic(semantic, context, ruleset=ruleset)
        if errors:
            print(
                json.dumps(
                    {"ok": False, "errors": errors, "warnings": warnings},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

        out = (
            _resolve_under_project(project, args.out)
            if args.out
            else project / ".astsight" / "mock_semantics.json"
        )
        save_json_atomic(out, semantic.model_dump(mode="json"))
    except Exception as exc:
        print(f"mock semantics 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    result = {
        "ok": True,
        "semantics": str(out),
        "context_id": semantic.context_id,
        "deps": len(semantic.deps),
        "branches": sum(len(dep.branches) for dep in semantic.deps),
        "gaps": len(semantic.gaps),
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Mock 语义: {result['semantics']}")
        print(
            f"  deps: {result['deps']}  分支: {result['branches']}  "
            f"gaps: {result['gaps']}"
        )
        for warning in warnings:
            print(f"  [WARN] {warning}")
        for gap in semantic.gaps:
            print(f"  [GAP] {gap.dep_id or 'global'}: {gap.reason}")
    return 0
