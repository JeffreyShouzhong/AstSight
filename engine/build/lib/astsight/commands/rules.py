"""CLI rules 命令的 service 实现。"""
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


def _rules_llm(config_path: str | None = None):
    """按配置文件 + 环境变量构造模型客户端；测试通过 monkeypatch 替换。"""
    from ..llm import LLMClient, load_config

    return LLMClient(load_config(config_path=config_path))


def _rule_source_documents(args) -> list[str]:
    """需求文档必填，设计文档可选；两者都作为 A1 的原文来源。"""
    return [*args.requirements, *getattr(args, "design", [])]


def _rules_extract(args) -> int:
    """需求文档是入口：确定性上下文 + 模型抽规则 + 确定性导入，一次完成。

    模型只产出候选草案；`rule_key`、A 级落地、冲突检测、CR 契约规则与
    版本号都由框架重算。校验不通过时 ruleset.json 保持原样。
    """
    from ..agent.rule_analysis import generate_rule_draft, rule_prompt_metadata

    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        context = build_rules_context(
            project,
            _rule_source_documents(args),
            openapi=args.openapi,
            prompt_path=args.prompt,
        )
        prepared = write_rules_context(
            context, project=project, out=args.out, prompt_path=args.prompt
        )
    except Exception as exc:
        print(f"rules extract 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    draft_path = prepared.context_dir / "rules.draft.json"
    llm = None
    try:
        llm = _rules_llm(getattr(args, "llm_config", None))
        draft = generate_rule_draft(
            context,
            llm=llm,
            prompt_path=args.prompt,
            max_rules=max(1, min(500, args.max_rules)),
        )
        # 草案先落盘再校验：校验失败时保留模型原始输出，便于定位是模型问题
        # 还是需求本身不可判定；规则库本身不写。
        save_json_atomic(draft_path, draft.model_dump(mode="json"))
    except Exception as exc:
        print(f"rules extract 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()

    # 规则库记录“哪版提示词、哪个模型”，便于审计同一需求的抽取口径。
    prompt_info = rule_prompt_metadata(context, llm)

    try:
        previous = load_rule_set(rule_base) if rule_base.is_file() else None
        result = import_rule_draft(
            project=project.name,
            context=context,
            draft=draft,
            previous=previous,
            prompt=prompt_info,
            dry_run=args.dry_run,
            path=rule_base,
        )
    except Exception as exc:
        print(f"rules extract 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = {
        "context_id": prepared.context_id,
        "context_dir": str(prepared.context_dir),
        "context": str(prepared.context_path),
        "prompt": str(prepared.prompt_path),
        "schema": str(prepared.schema_path),
        "draft": str(draft_path),
        "rule_base": str(rule_base),
        "written": result.written,
        "version": result.ruleset.version,
        "business_rules": len(result.ruleset.rules),
        "contract_rules": len(result.ruleset.contract_rules),
        "contract_sources": {
            kind: sum(
                1
                for rule in result.ruleset.contract_rules
                if rule.source_kind == kind
            )
            for kind in sorted(
                {rule.source_kind for rule in result.ruleset.contract_rules}
            )
        },
        "questions": len(result.ruleset.questions),
        "gaps": len(result.ruleset.gaps),
        "conflicts": [conflict.topic for conflict in result.ruleset.conflicts],
        "validation": result.validation.as_dict(),
        "diff": result.diff.as_dict(),
        "warnings": [
            *(context.get("warnings") or []),
            *result.validation.warnings,
        ],
    }
    if result.written:
        # 人工评审文档只读：用于和产品方对齐需求缺陷，不参与导入。
        review_path = rule_base.with_suffix(".md")
        review_path.write_text(
            render_rule_set_markdown(result.ruleset), encoding="utf-8"
        )
        payload["review"] = str(review_path)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if result.validation.errors:
        return 1
    return 0


def _rules_prepare(args) -> int:
    project = Path(args.project).resolve()
    try:
        prepared = prepare_rules_context(
            project,
            _rule_source_documents(args),
            openapi=args.openapi,
            out=args.out,
            prompt_path=args.prompt,
        )
    except Exception as exc:
        print(f"rules prepare 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = {
        "context_id": prepared.context_id,
        "context_dir": str(prepared.context_dir),
        "context": str(prepared.context_path),
        "prompt": str(prepared.prompt_path),
        "schema": str(prepared.schema_path),
        "documents": len(prepared.context.get("documents") or []),
        "operations": len(prepared.context.get("operations") or []),
        "contract": prepared.context.get("contract_summary") or {},
        "warnings": prepared.context.get("warnings") or [],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _rules_import(args) -> int:
    from ..agent.rule_analysis import rule_prompt_metadata

    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        context = load_rules_context(args.context)
        draft = load_rule_draft(args.draft)
        previous = load_rule_set(rule_base) if rule_base.is_file() else None
        result = import_rule_draft(
            project=project.name,
            context=context,
            draft=draft,
            previous=previous,
            prompt=rule_prompt_metadata(context),
            dry_run=args.dry_run,
            path=rule_base,
        )
    except (RuleStoreError, Exception) as exc:
        print(f"rules import 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = result.as_dict()
    payload["rule_base"] = str(rule_base)
    if result.written:
        # 人工评审文档是本次抽取的只读视图：随规则库一起产出，
        # 用来和产品方对齐需求缺陷；它不参与导入，也不能反向修改规则。
        review_path = rule_base.with_suffix(".md")
        review_path.write_text(
            render_rule_set_markdown(result.ruleset), encoding="utf-8"
        )
        payload["review"] = str(review_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if result.validation.errors:
        return 1
    return 0


def _rules_status(args) -> int:
    from ..cases.store import approved_cases

    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        ruleset = load_rule_set(rule_base)
    except Exception as exc:
        print(f"rules status 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    status_counts: dict[str, int] = {}
    authority_counts: dict[str, int] = {}
    for rule in ruleset.all_rules():
        status_counts[rule.status.value] = status_counts.get(rule.status.value, 0) + 1
        authority_counts[rule.authority.value] = authority_counts.get(rule.authority.value, 0) + 1
    case_base = _resolve_under_project(project, args.case_base) or project / "caseset.json"
    caseset = load_case_set(case_base) if case_base.is_file() else None
    coverage = None
    orphaned: dict[str, list[str]] = {}
    if caseset is not None:
        cases = approved_cases(caseset)
        coverage = rule_coverage(ruleset, cases).as_dict()
        known = ruleset.rule_index()
        for entry in cases:
            for ref in entry.rule_refs:
                if ref.rule_key not in known:
                    orphaned.setdefault(ref.rule_key, []).append(entry.id)
    payload = {
        "rule_base": str(rule_base),
        "ruleset_id": ruleset.ruleset_id,
        "version": ruleset.version,
        "hash": rule_set_hash(ruleset),
        "business_rules": len(ruleset.rules),
        "contract_rules": len(ruleset.contract_rules),
        "contract_sources": {
            kind: sum(
                1
                for rule in ruleset.contract_rules
                if rule.source_kind == kind
            )
            for kind in sorted(
                {rule.source_kind for rule in ruleset.contract_rules}
            )
        },
        "status": status_counts,
        "authority": authority_counts,
        "questions": [item.model_dump(mode="json") for item in ruleset.questions],
        "gaps": [item.model_dump(mode="json") for item in ruleset.gaps],
        "conflicts": [
            item.model_dump(mode="json") for item in ruleset.conflicts
        ],
        "conflict_count": len(ruleset.conflicts),
        "rule_drift_stale_cases": orphaned,
        "case_base": str(case_base) if caseset is not None else None,
        "coverage": coverage,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _rules_render(args) -> int:
    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        ruleset = load_rule_set(rule_base)
    except Exception as exc:
        print(f"rules render 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    markdown = render_rule_set_markdown(ruleset)
    if args.stdout:
        print(markdown)
        return 0

    out = _resolve_under_project(project, args.out) or rule_base.with_suffix(".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    payload = {
        "rule_base": str(rule_base),
        "out": str(out),
        "version": ruleset.version,
        "rules": len(ruleset.rules),
        "conflicts": len(ruleset.conflicts),
        "gaps": len(ruleset.gaps),
        "questions": len(ruleset.questions),
        "bytes": len(markdown.encode("utf-8")),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"已渲染 {out}")
    return 0
