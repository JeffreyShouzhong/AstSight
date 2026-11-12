"""端到端 Agent 流程（第一版）：

    代码变更 → 接口识别 → A1 语义分析 → A4 用例草案 → 校验/审核
    → Mock 计划与运行时 → 注入并启动 SUT → 执行 → A5 诊断 → A6 修复（有界）

边界：
- 用例必须经过 proposal；`approve_as` 只自动批准 new/update/revive，
  deprecate 永远 reject（人工逐项批准）。
- 修复只作用于未批准草案的新一轮 proposal，不直接改已批准用例。
- 模型输出全部按不可信输入处理，证据校验失败即中止。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from contracts import DataCatalog, DataPlanBundle, MockSemantic, RuleSet

from ..artifacts import RunDir, new_run_id, utc_now
from ..cases.context import build_context
from ..cases.proposal import (
    approve_proposal,
    build_proposal,
    list_pending_proposals,
)
from ..cases.store import (
    approved_cases,
    case_summary,
    empty_case_set,
    load_case_set,
    requirement_coverage,
    resolve_case_base,
    store_hash,
)
from ..cases.validate import validate_draft
from ..changes.git_diff import diff_files, repo_root, resolve_range
from ..changes.impact import analyze_impact
from ..changes.symbol_diff import symbols_for_changes
from ..data import (
    build_catalog,
    build_data_plan_bundle,
    dialect_from_url,
    load_seed,
    normalize_dialect,
    parse_ddl,
    validate_catalog,
)
from ..inventory import (
    discover_openapi,
    inventory_routes,
    route_inventory_health,
)
from ..llm import LLMClient, load_config
from ..mock import MockPlanError, start_mock_plan
from ..mock.semantic import build_mock_semantic
from ..persistence import load_json, save_json_atomic
from ..pipeline import RunConfig, execute_cases, execute_with_runtime
from ..profile import load_spec, parse_operations
from ..profile.openapi import normalize_path
from ..rules import (
    build_rules_context,
    load_rule_set,
    render_rule_set_markdown,
    save_rule_set,
    write_rules_context,
)
from .case_design import generate_case_design
from .data_prep import generate_data_plans, generate_data_semantics
from .diagnosis import DiagnosisOutcome, diagnose_failures
from .errors import AgentError
from .mock_semantics import generate_mock_semantic_candidates
from .mockplan import build_mock_plan
from .repair import propose_repairs
from .rule_analysis import (
    generate_rule_draft,
    import_rule_draft_for_flow,
    rule_prompt_metadata,
)
from .sut import start_sut

_APPROVE_ACTIONS = {"new", "update", "revive"}
_SUT_ID_RE = re.compile(r"[^0-9A-Za-z_.-]+")


@dataclass
class FlowConfig:
    project: str
    requirements: list[str]
    openapi: str | None = None
    llm_config: str | None = None
    mode: str = "full"                 # full | delta
    base: str = "main"
    target: str = "HEAD"
    depth: int = 5
    base_url: str | None = None
    reset_url: str | None = None
    codegraph: str = "auto"
    codegraph_module_root: str | None = None
    full_index: bool | None = None
    case_base: str | None = None
    out: str = ".astsight/agent"
    approve_as: str | None = None
    max_repair_rounds: int = 0
    max_cases: int = 200
    sut_command: list[str] | None = None
    sut_ready_url: str | None = None
    sut_env: dict[str, str] = field(default_factory=dict)
    sut_timeout: float = 60.0
    timeout: float = 600.0
    retries: int = 1
    collect_only: bool = True
    skip_execute: bool = False
    allow_unmapped: bool = True
    min_pass_rate: float = 0.95
    mock_host: str = "127.0.0.1"
    mock_port: int = 0
    rule_base: str | None = None
    data_ddl: list[str] = field(default_factory=list)
    data_seed: str | None = None
    data_catalog: str | None = None
    data_plan: str | None = None
    database_url: str | None = None
    data_dialect: str | None = None
    skip_data: bool = False


@dataclass
class FlowResult:
    run_id: str
    run_dir: Path
    status: str
    steps: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report: dict[str, Any] | None = None
    llm_usage: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "status": self.status,
            "steps": self.steps,
            "warnings": self.warnings,
            "errors": self.errors,
            "report": self.report,
            "llm_usage": self.llm_usage,
        }


def _sut_id(project: Path) -> str:
    cleaned = _SUT_ID_RE.sub("-", project.name).strip("-._")
    return (cleaned or "sut")[:64]


def _full_scope(routes: list[dict]) -> tuple[list[dict], dict]:
    changed_apis: list[dict] = []
    for route in routes:
        method = (route.get("method") or "").upper()
        if not method:
            continue
        handler = route.get("handler", "")
        changed_apis.append(
            {
                "handler": f"{method} {route.get('path')}",
                "method": method,
                "path": route.get("path", ""),
                "route_handler": handler,
                "confidence": "high",
                "evidence": route.get("confidence", "verified"),
                "changed_symbols": [],
                "change_files": [route.get("file", "")],
                "trace": [handler] if handler else [],
                "route_source": route.get("source", ""),
                "scope": "full",
            }
        )
    return changed_apis, {
        "mode": "full",
        "note": "full 模式扫描全量路由，不依赖 git diff",
        "changed_files": 0,
    }


def _delta_scope(
    project: Path, config: FlowConfig, routes: list[dict], graph_client: Any
) -> tuple[list[dict], dict, list[dict]]:
    root = repo_root(project)
    if root != project:
        raise AgentError(
            "delta_project_not_repo_root",
            f"delta 模式要求 --project 指向仓库根，当前为子目录: {project}（根: {root}）",
        )
    range_arg, comparison = resolve_range(project, config.base, config.target)
    file_changes, changes_meta = diff_files(project, config.base, config.target)
    changes_meta["comparison"] = comparison
    changes_meta["range"] = range_arg
    changed_symbols, unsupported = symbols_for_changes(
        project,
        config.base,
        config.target,
        file_changes,
        codegraph_client=graph_client,
    )
    summary = analyze_impact(
        changed_symbols,
        routes,
        graph_client,
        depth=config.depth,
        project_root=project,
    )
    impact = summary.as_dict()
    impact["changed_symbols"] = [symbol.as_dict() for symbol in changed_symbols]
    impact["unsupported_files"] = unsupported
    changes_meta.update(
        {
            "files": [item.as_dict() for item in file_changes],
            "symbols": [symbol.as_dict() for symbol in changed_symbols],
            "unsupported_files": unsupported,
        }
    )
    return impact.get("changed_apis") or [], changes_meta, unsupported


def _align_to_openapi(changed_apis: list[dict], operations: list[Any]) -> None:
    index = {
        (op.method.upper(), normalize_path(op.path)): op for op in operations
    }
    for api in changed_apis:
        operation = index.get(
            ((api.get("method") or "").upper(), normalize_path(api.get("path", "")))
        )
        if operation is not None and operation.path != api.get("path"):
            api["path_aligned_from"] = api.get("path", "")
            api["path"] = operation.path
            api["handler"] = f"{api.get('method', '').upper()} {operation.path}"


def _profile_routes(changed_apis: list[dict]) -> list[dict]:
    return [
        {
            "method": api.get("method", ""),
            "path": api.get("path", ""),
            "handler": api.get("route_handler", ""),
            "owner": api.get("route_owner", ""),
            "file": api.get("route_file") or (api.get("change_files") or [""])[0],
            "source": api.get("route_source", ""),
            "confidence": api.get("evidence", "inferred"),
        }
        for api in changed_apis
    ]


def _fill_decisions(review_path: Path, reviewer: str) -> dict[str, int]:
    """只自动批准新增/更新/恢复；废弃项一律 reject，等待人工逐项批准。"""
    review = json.loads(review_path.read_text(encoding="utf-8"))
    counts = {"approve": 0, "reject": 0}
    for item in review.get("items") or []:
        action = str(item.get("action") or "")
        if action in _APPROVE_ACTIONS:
            item["decision"] = "approve"
            item["note"] = "agent flow: 自动批准（人工审核入口仍保留）"
            counts["approve"] += 1
        else:
            item["decision"] = "reject"
            item["note"] = f"agent flow: {action} 必须人工逐项批准"
            counts["reject"] += 1
        item["reviewer"] = reviewer
    review_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return counts


def _scope_cases(store: Any, draft: Any) -> list[Any]:
    allowed = {
        (case.endpoint, case.case_key)
        for case in getattr(draft, "cases", []) or []
    }
    return [
        entry
        for entry in approved_cases(store)
        if (entry.endpoint, entry.case_key) in allowed
    ]


def _store_state(store: Any, store_path: Path) -> dict[str, Any]:
    return {
        "case_base": str(store_path),
        "store_version": store.store_version,
        "store_hash": store_hash(store),
        "approved_at": store.approved_at,
        "approved_by": store.approved_by,
        "summary": case_summary(store),
        "pending_proposals": 0,
        "pending_proposal_paths": [],
        "degraded": False,
        "contract_drift": {"checked": False, "changed": False, "reason": "agent_flow"},
        "requirement_drift": {
            "checked": False,
            "changed": False,
            "changed_docs": [],
            "missing_docs": [],
        },
        "requirement_coverage": requirement_coverage(store),
        "review_log_tail": [
            event.model_dump(mode="json") for event in store.review_log[-10:]
        ],
        "gate_blockers": [],
        "gate_warnings": [],
    }


def _execution_config(
    config: FlowConfig, data_plan_path: Path | None = None
) -> RunConfig:
    return RunConfig(
        project=config.project,
        mode="cases",
        base_url=config.base_url,
        reset_url=config.reset_url,
        openapi=config.openapi,
        timeout=config.timeout,
        retries=config.retries,
        collect_only=config.collect_only,
        skip_execute=config.skip_execute,
        min_pass_rate=config.min_pass_rate,
        allow_unmapped=config.allow_unmapped,
        codegraph="off",
        case_base=config.case_base,
        data_plan=str(data_plan_path) if data_plan_path else None,
        database_url=config.database_url,
        ruleset=config.rule_base or "ruleset.json",
    )


def _read_entries(run_dir: RunDir) -> list[dict[str, Any]]:
    path = run_dir.path / "execution.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(payload.get("records") or [])


def _read_mock_summary(run_dir: RunDir) -> dict[str, Any] | None:
    path = run_dir.path / "mock_evidence.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _execute_round(
    *,
    project: Path,
    config: FlowConfig,
    run_dir: RunDir,
    run_meta: dict[str, Any],
    impact: dict[str, Any],
    profiles: list[dict],
    cases: list[Any],
    unmapped: dict[str, Any],
    route_audit: dict[str, Any],
    store: Any,
    store_path: Path,
    runtime: Any,
    data_plan_path: Path | None = None,
) -> dict[str, Any]:
    kwargs = dict(
        project=project,
        config=_execution_config(config, data_plan_path),
        run_dir=run_dir,
        run_meta=run_meta,
        changes={
            "mode": "agent",
            "case_base": str(store_path),
            "store_version": store.store_version,
        },
        impact=impact,
        profiles=profiles,
        cases=cases,
        unmapped=unmapped,
        route_audit=route_audit,
        case_store_state=_store_state(store, store_path),
        codegen_gate={
            "allow_unmapped": config.allow_unmapped,
            "min_pass_rate": config.min_pass_rate,
        },
    )
    if runtime is None:
        return execute_cases(**kwargs)
    return execute_with_runtime(**kwargs, runtime=runtime)


def _changed_endpoints(impact: dict[str, Any]) -> set[str]:
    return {
        str(api.get("handler") or "")
        for api in impact.get("changed_apis") or []
        if api.get("handler")
    }


def _diagnose(
    *,
    run_dir: RunDir,
    cases: list[Any],
    llm: LLMClient,
    impact: dict[str, Any],
) -> tuple[Any, list[dict[str, Any]]]:
    entries = _read_entries(run_dir)
    try:
        outcome = diagnose_failures(
            cases=cases,
            entries=entries,
            llm=llm,
            mock_summary=_read_mock_summary(run_dir),
            changed_endpoints=_changed_endpoints(impact),
        )
    except AgentError as exc:
        outcome = DiagnosisOutcome(errors=[f"{exc.code}: {exc.detail}"])
    run_dir.write_json("diagnosis.json", outcome.by_case)
    run_dir.write_json(
        "diagnosis_meta.json",
        {
            "eligible_case_ids": sorted(outcome.eligible_case_ids),
            "errors": outcome.errors,
            "warnings": outcome.warnings,
        },
    )
    return outcome, entries


def _project_path(project: Path, raw: str | None, default: str) -> Path:
    path = Path(raw or default)
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _project_inputs(project: Path, values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in values:
        path = Path(raw)
        if not path.is_absolute():
            path = project / path
        path = path.resolve()
        if not path.is_relative_to(project):
            raise AgentError("data_ddl_outside_project", str(path))
        if not path.is_file():
            raise AgentError("data_ddl_missing", str(path))
        paths.append(path)
    return paths


def _configured_data_dialect(config: FlowConfig) -> str | None:
    """显式 --db-dialect 优先，其次从 --db-url 推断；都没有时返回 None。"""
    try:
        if config.data_dialect:
            return normalize_dialect(config.data_dialect)
        if config.database_url:
            return dialect_from_url(config.database_url)
    except ValueError as exc:
        raise AgentError("data_dialect_invalid", str(exc)) from exc
    return None


def _build_data_catalog(
    *,
    project: Path,
    context: dict[str, Any],
    config: FlowConfig,
) -> DataCatalog | None:
    configured = _configured_data_dialect(config)
    if config.data_catalog:
        path = _project_path(project, config.data_catalog, ".astsight/data/data_catalog.json")
        if not path.is_file():
            raise AgentError("data_catalog_missing", str(path))
        catalog = DataCatalog.model_validate(load_json(path))
        errors = validate_catalog(catalog)
        if errors:
            raise AgentError("data_catalog_invalid", "；".join(errors[:20]))
        if configured and catalog.dialect != configured:
            raise AgentError(
                "data_dialect_mismatch",
                f"data_catalog 为 {catalog.dialect}，但目标数据库为 {configured}",
            )
        return catalog
    if not config.data_ddl:
        return None
    dialect = configured or "sqlite"
    ddl_paths = _project_inputs(project, config.data_ddl)
    text = "\n;\n".join(
        path.read_text(encoding="utf-8-sig") for path in ddl_paths
    )
    parsed = parse_ddl(text, dialect=dialect)
    seed_path = (
        _project_path(project, config.data_seed, config.data_seed)
        if config.data_seed
        else None
    )
    seed = load_seed(seed_path, parsed)
    catalog = build_catalog(
        project=project.name,
        ddl=parsed,
        seed=seed,
        context_id=str(context.get("context_id") or ""),
        dialect=dialect,
        sources={
            "ddl": [
                path.relative_to(project).as_posix() for path in ddl_paths
            ],
            "seed": (
                seed_path.relative_to(project).as_posix()
                if seed_path is not None and seed_path.is_relative_to(project)
                else str(seed_path) if seed_path else None
            ),
        },
    )
    errors = validate_catalog(catalog)
    if errors:
        raise AgentError("data_catalog_invalid", "；".join(errors[:20]))
    return catalog


def run_flow(
    config: FlowConfig,
    *,
    llm: LLMClient | None = None,
) -> FlowResult:
    project = Path(config.project).resolve()
    if not project.is_dir():
        raise AgentError("project_missing", f"项目目录不存在: {project}")
    if not config.requirements:
        raise AgentError("requirements_missing", "至少提供一个 --requirements")

    owned_llm = llm is None
    client = llm or LLMClient(load_config(config_path=config.llm_config))

    run_id = new_run_id("agent")
    out_root = (
        Path(config.out)
        if Path(config.out).is_absolute()
        else project / config.out
    )
    run_dir = RunDir(out_root, run_id)
    result = FlowResult(run_id=run_id, run_dir=run_dir.path, status="not_run")
    steps: dict[str, Any] = {}
    result.steps = steps

    runtime = None
    sut = None
    data_plan_file: Path | None = None
    try:
        # ── 1. 路由 + 变更范围 ────────────────────────────────
        # 契约不是流程前置条件：需求先产出规则，接口事实从代码路由补；
        # 有 OpenAPI 才额外获得字段/必填/枚举/错误码等契约事实。
        openapi_source = discover_openapi(project, config.openapi, None)
        operations: list[Any] = []
        if openapi_source:
            operations = parse_operations(load_spec(openapi_source))
        else:
            result.warnings.append(
                "openapi_missing: 未提供/未发现 OpenAPI；本轮只使用代码路由构建画像，"
                "CR 契约规则为空，字段格式类用例无法生成"
            )

        routes, route_audit, graph_client, graph_notes = inventory_routes(
            project,
            codegraph=config.codegraph,
            codegraph_module_root=config.codegraph_module_root,
            full_index=config.full_index,
            openapi_source=openapi_source,
        )
        supplement = (route_audit or {}).get("openapi_supplement") or {}
        route_audit = {
            **(route_audit or {}),
            "health": route_inventory_health(
                project, routes, openapi_operations=supplement.get("operations")
            ),
        }

        if config.mode == "delta":
            changed_apis, changes_meta, unsupported = _delta_scope(
                project, config, routes, graph_client
            )
        else:
            changed_apis, changes_meta = _full_scope(routes)
            unsupported = []
        _align_to_openapi(changed_apis, operations)
        impact = {
            "changed_apis": changed_apis,
            "unresolved_symbols": [],
            "traces": [],
            "changed_symbols": [],
            "unsupported_files": unsupported,
            "scope": config.mode,
        }
        run_dir.write_json("routes.json", {"routes": routes, "audit": route_audit})
        run_dir.write_json("changed_apis.json", impact)
        run_dir.write_json("changes.json", changes_meta)
        steps["scope"] = {
            "mode": config.mode,
            "routes": len(routes),
            "changed_apis": [api.get("handler") for api in changed_apis],
            "openapi": openapi_source,
            "interface_evidence": "openapi" if operations else "code_routes_only",
        }
        if not changed_apis and (config.mode != "full" or routes):
            result.warnings.append("本次变更未命中任何接口，流程结束")
            result.status = "not_run"
            return result
        if not routes:
            result.warnings.append(
                "routes_missing: 未从代码或 OpenAPI 发现可用接口；"
                "本轮先完成需求规则抽取，不生成用例"
            )

        # ── 2. 用例上下文（画像）────────────────────────────
        context = build_context(
            project,
            config.requirements,
            openapi=openapi_source,
            routes=_profile_routes(changed_apis),
            changed_apis=changed_apis,
            codegraph_client=graph_client,
        )
        run_dir.write_json("context.json", context)
        result.warnings.extend(
            f"context:{warning}" for warning in context.get("warnings") or []
        )
        steps["scope"]["profiles"] = len(context.get("profiles") or [])
        steps["scope"]["contract_incomplete"] = sum(
            1
            for profile in context.get("profiles") or []
            if profile.get("contract_incomplete")
        )

        # ── 3. A1 规则抽取（LLM 候选 + 确定性导入）──────────
        rules_context = build_rules_context(
            project, config.requirements, openapi=openapi_source
        )
        rules_context_dir = write_rules_context(
            rules_context, project=project, out=run_dir.path / "rules"
        )
        rule_base = _project_path(project, config.rule_base, "ruleset.json")
        previous_ruleset = load_rule_set(rule_base) if rule_base.is_file() else None
        rule_draft = generate_rule_draft(rules_context, llm=client)
        run_dir.write_json(
            "rules.draft.json", rule_draft.model_dump(mode="json")
        )
        imported = import_rule_draft_for_flow(
            project=project.name,
            context=rules_context,
            draft=rule_draft,
            previous_path=rule_base if rule_base.is_file() else None,
            prompt=rule_prompt_metadata(rules_context, client),
        )
        save_rule_set(rule_base, imported.ruleset)
        # 人工评审文档随规则库产出：用于和产品方对齐需求缺陷，只读、不参与导入。
        rules_review_path = rule_base.with_suffix(".md")
        rules_review_path.write_text(
            render_rule_set_markdown(imported.ruleset), encoding="utf-8"
        )
        result.warnings.extend(imported.validation.warnings)
        conflicted = [conflict.topic for conflict in imported.ruleset.conflicts]
        if conflicted:
            result.warnings.append(
                f"规则冲突（将在用例审核中确认）: {', '.join(conflicted[:5])}"
            )
        steps["rules"] = {
            "ruleset": str(rule_base),
            "review": str(rules_review_path),
            "context_dir": str(rules_context_dir.context_dir),
            "context_id": rules_context_dir.context_id,
            "business_rules": len(imported.ruleset.rules),
            "contract_rules": len(imported.ruleset.contract_rules),
            "diff": imported.diff.as_dict(),
            "questions": len(imported.ruleset.questions),
            "gaps": len(imported.ruleset.gaps),
            "conflicts": conflicted,
            "warnings": imported.validation.warnings,
        }
        ruleset = load_rule_set(rule_base)

        if not routes:
            # 没有接口事实就不能生成可执行用例；规则库仍然完整产出并落盘。
            result.status = "not_run"
            return result

        # ── 4. C3.5.1 Mock 语义（契约层 + 规则映射）──────────
        mock_semantic = build_mock_semantic(context, ruleset)
        mock_candidates = generate_mock_semantic_candidates(
            context, mock_semantic, ruleset=ruleset, llm=client
        )
        if mock_candidates is not None:
            result.warnings.extend(mock_candidates.warnings)
        mock_semantic_path = _project_path(
            project, ".astsight/mock_semantics.json", ".astsight/mock_semantics.json"
        )
        save_json_atomic(
            mock_semantic_path, mock_semantic.model_dump(mode="json")
        )
        run_dir.write_json(
            "mock_semantics.json", mock_semantic.model_dump(mode="json")
        )
        if mock_semantic.gaps:
            result.warnings.extend(
                f"mock_semantic_gap:{gap.dep_id or 'global'}:{gap.reason}"
                for gap in mock_semantic.gaps[:20]
            )
        steps["mock_semantics"] = {
            "path": str(mock_semantic_path),
            "deps": len(mock_semantic.deps),
            "branches": sum(len(dep.branches) for dep in mock_semantic.deps),
            "gaps": len(mock_semantic.gaps),
        }

        # ── 5. C5a 数据目录（DDL + seed + LLM 语义候选）──────
        catalog = _build_data_catalog(
            project=project, context=context, config=config
        )
        if catalog is not None:
            if not config.skip_data and client is not None:
                semantic_draft, applied = generate_data_semantics(
                    catalog,
                    llm=client,
                    ruleset=ruleset,
                    context=rules_context,
                )
                catalog = applied.catalog
                result.warnings.extend(applied.warnings)
                run_dir.write_json(
                    "data_semantics.draft.json",
                    semantic_draft.model_dump(mode="json"),
                )
            catalog_path = _project_path(
                project, ".astsight/data/data_catalog.json", ".astsight/data/data_catalog.json"
            )
            save_json_atomic(catalog_path, catalog.model_dump(mode="json"))
            run_dir.write_json(
                "data_catalog.json", catalog.model_dump(mode="json")
            )
            steps["data_catalog"] = {
                "path": str(catalog_path),
                "catalog_id": catalog.catalog_id,
                "tables": len(catalog.tables),
                "states": len(catalog.states),
                "gaps": len(catalog.gaps),
            }

        # ── 6. A4 用例设计（规则 + 画像 + 语义 + 造数目录）──
        design = generate_case_design(
            context,
            ruleset=ruleset,
            llm=client,
            project=project,
            mode=config.mode,
            changed_apis=changed_apis,
            mock_semantic=mock_semantic,
            data_catalog=catalog,
            max_cases=config.max_cases,
        )
        draft = design.draft
        validation = design.validation
        draft_path = run_dir.path / "cases.draft.json"
        save_json_atomic(draft_path, draft.model_dump(mode="json"))
        result.warnings.extend(design.warnings)
        steps["case_design"] = {
            "cases": len(draft.cases),
            "business_cases": sum(
                1 for case in draft.cases if case.level.value == "business"
            ),
            "code_cases": sum(
                1 for case in draft.cases if case.level.value == "code"
            ),
            "warnings": design.warnings,
        }

        # ── 7. proposal / 审核 ───────────────────────────────
        store_path = resolve_case_base(project, config.case_base)
        store = load_case_set(store_path) or empty_case_set(project, context=context)
        built = build_proposal(
            project=project,
            draft_path=draft_path,
            draft=draft,
            context=context,
            validation=validation,
            store=store,
            store_path=store_path,
            ruleset=ruleset,
            data_catalog=catalog,
        )
        steps["proposal"] = {
            "path": str(built.path) if built.path else None,
            "diff": built.diff.counts(),
            "reused": built.reused,
        }
        if built.path is not None:
            if not config.approve_as:
                result.warnings.append(
                    f"存在待审 proposal：{built.path}；未提供 --approve-as，流程停在人工审核"
                )
                result.status = "pending_review"
                return result
            decisions = _fill_decisions(
                built.path / "cases.review.json", config.approve_as
            )
            steps["proposal"]["decisions"] = decisions
            approve_proposal(
                proposal_path=built.path,
                project=project,
                case_base=store_path,
                reviewer=config.approve_as,
            )
            store = load_case_set(store_path)
            if store is None:
                raise AgentError("case_store_missing", str(store_path))
        cases = _scope_cases(store, draft)
        if not cases:
            result.warnings.append("审核后没有可执行的用例")
            result.status = "not_run"
            return result
        steps["approved_cases"] = [case.id for case in cases]
        pending = list_pending_proposals(project)
        if pending:
            result.warnings.append(f"仍有 {len(pending)} 个待审 proposal 未应用")

        # ── 8. C5b 造数计划（批准后，LLM 候选 + 确定性校验）──
        data_bundle: DataPlanBundle | None = None
        if catalog is not None and not config.skip_data:
            if config.data_plan:
                plan_path = _project_path(
                    project, config.data_plan, ".astsight/data/data_plan.json"
                )
                if not plan_path.is_file():
                    raise AgentError("data_plan_missing", str(plan_path))
                data_bundle = DataPlanBundle.model_validate(load_json(plan_path))
            else:
                plan_draft = generate_data_plans(
                    cases,
                    catalog=catalog,
                    ruleset=ruleset,
                    llm=client,
                    mock_semantic=mock_semantic,
                )
                run_dir.write_json(
                    "data_plan.draft.json", plan_draft.model_dump(mode="json")
                )
                data_bundle = build_data_plan_bundle(
                    cases=cases,
                    catalog=catalog,
                    ruleset=ruleset,
                    payloads=plan_draft.plans,
                    run_scope=run_id,
                )
            plan_path = _project_path(
                project, ".astsight/data/data_plan.json", ".astsight/data/data_plan.json"
            )
            save_json_atomic(
                plan_path, data_bundle.model_dump(mode="json")
            )
            data_plan_file = plan_path
            run_dir.write_json(
                "data_plan.json", data_bundle.model_dump(mode="json")
            )
            steps["data_plan"] = {
                "path": str(plan_path),
                "run_scope": data_bundle.run_scope,
                "plans": len(data_bundle.plans),
                "rejected": [
                    item.model_dump(mode="json") for item in data_bundle.rejected
                ],
            }
            result.warnings.extend(data_bundle.warnings)
            if data_bundle.rejected:
                raise AgentError(
                    "data_plan_invalid",
                    "；".join(
                        f"{item.case_key}:{item.reason}"
                        for item in data_bundle.rejected[:20]
                    ),
                    details={
                        "rejected": [
                            item.model_dump(mode="json")
                            for item in data_bundle.rejected
                        ]
                    },
                )
        elif any(case.preconditions for case in cases):
            result.warnings.append(
                "data_unavailable: 用例含前置状态但未提供 DDL/data catalog，"
                "相关用例应按 blocked 处理"
            )

        # ── 9. Mock 计划与运行时 ─────────────────────────────
        plan, mock_warnings = build_mock_plan(
            context,
            mock_semantic,
            SimpleNamespace(cases=cases),
            sut_id=_sut_id(project),
            host=config.mock_host,
            port=config.mock_port,
        )
        result.warnings.extend(mock_warnings)
        injection_document: dict[str, Any] = {"injections": []}
        if plan is not None:
            plan_path = run_dir.path / "mock_plan.json"
            save_json_atomic(plan_path, plan.model_dump(mode="json"))
            try:
                runtime = start_mock_plan(
                    plan_path, history_path=run_dir.path / "mock_calls.jsonl"
                )
            except MockPlanError as exc:
                raise AgentError(
                    "mock_start_failed", f"{exc.code}: {exc.detail}"
                ) from exc
            injection_document = runtime.injection_document()
            run_dir.write_json(
                "run_manifest.json", runtime.manifest(run_id=run_id)
            )
            run_dir.write_json("mock_injection.json", injection_document)
        steps["mock"] = {
            "started": runtime is not None,
            "injections": len(injection_document.get("injections") or []),
            "warnings": mock_warnings,
        }

        # ── 6. SUT 启动（先 Mock、后 SUT）────────────────────
        if config.sut_command:
            if not config.base_url:
                raise AgentError(
                    "sut_base_url_missing",
                    "--sut-command 需要同时提供 --base-url（就绪探测与用例执行使用）",
                )
            sut = start_sut(
                config.sut_command,
                base_url=config.base_url,
                log_path=run_dir.path / "sut.log",
                injection_document=injection_document,
                extra_env=config.sut_env,
                ready_url=config.sut_ready_url,
                timeout=config.sut_timeout,
                cwd=project,
            )
            result.warnings.extend(sut.warnings)
            steps["sut"] = {
                "started": True,
                "env_keys": sut.env_keys,
                "log": str(sut.log_path),
            }
        elif runtime is not None and injection_document.get("injections"):
            result.warnings.append(
                "Mock 已启动但未提供 --sut-command：注入未应用到 SUT，"
                "需要外部自行设置环境变量"
            )

        # ── 10. 造数落地（用例级 fixture）────────────────────
        # apply/teardown 由生成物中的 data_state fixture 按用例执行，
        # 不在编排层整批预插；缺 DB 时用例会被标 blocked，而不是静默通过。
        if data_bundle is not None and data_bundle.plans:
            steps["data_apply"] = {
                "mode": "per_case",
                "plans": len(data_bundle.plans),
                "database_url_configured": bool(config.database_url),
            }
            if not config.database_url:
                result.warnings.append(
                    "data_unavailable: 已生成 data_plan，但未提供数据库 URL；"
                    "依赖前置状态的用例将标 blocked"
                )
        elif any(case.preconditions for case in cases):
            steps["data_apply"] = {
                "mode": "blocked",
                "plans": 0,
                "database_url_configured": bool(config.database_url),
            }

        # ── 11. 执行 ────────────────────────────────────────
        run_meta = {
            "run_id": run_id,
            "mode": "agent",
            "project": str(project),
            "started_at": utc_now(),
            "base": config.base if config.mode == "delta" else None,
            "target": config.target if config.mode == "delta" else None,
            "base_url": config.base_url,
            "openapi": openapi_source,
            "scope": config.mode,
            "approve_as": config.approve_as,
        }
        unmapped = {
            "symbols": [],
            "routes": [],
            "profile_unmapped": [
                {
                    "endpoint": item.get("endpoint", ""),
                    "handler": item.get("handler", ""),
                    "reason": item.get("reason", ""),
                    "file": item.get("file", ""),
                }
                for item in context.get("profile_unmapped") or []
            ],
            "unsupported_files": unsupported,
        }
        report = _execute_round(
            project=project,
            config=config,
            run_dir=run_dir,
            run_meta=run_meta,
            impact=impact,
            profiles=context.get("profiles") or [],
            cases=cases,
            unmapped=unmapped,
            route_audit=route_audit,
            store=store,
            store_path=store_path,
            runtime=runtime,
            data_plan_path=data_plan_file,
        )
        result.report = report
        result.status = str((report.get("gate") or {}).get("verdict") or "not_run")
        steps["execution"] = {
            "run_dir": str(run_dir.path),
            "report": str(run_dir.path / "report.json"),
            "verdict": result.status,
        }

        # ── 8. 诊断 + 有界修复 ───────────────────────────────
        diagnosis, entries = _diagnose(
            run_dir=run_dir, cases=cases, llm=client, impact=impact
        )
        result.warnings.extend(diagnosis.warnings)
        steps["diagnosis"] = {
            "cases": len(diagnosis.by_case),
            "eligible_for_repair": sorted(diagnosis.eligible_case_ids),
            "errors": diagnosis.errors,
        }
        if diagnosis.errors:
            result.errors.extend(diagnosis.errors)

        key_by_id = {case.id: case.case_key for case in cases}
        for round_no in range(1, max(0, config.max_repair_rounds) + 1):
            if not diagnosis.eligible_case_ids:
                break
            try:
                repair = propose_repairs(
                    draft=draft,
                    failures=entries,
                    diagnoses=diagnosis.by_case,
                    context=context,
                    analysis=None,
                    llm=client,
                    project=project,
                    case_keys=key_by_id,
                    ruleset=ruleset,
                    data_catalog=catalog,
                    mock_semantic=mock_semantic,
                )
            except AgentError as exc:
                result.warnings.append(
                    f"修复建议失败，停止修复：{exc.code}: {exc.detail}"[:500]
                )
                steps[f"repair_{round_no}"] = {"error": exc.code}
                break
            if not repair.changed:
                steps[f"repair_{round_no}"] = {
                    "applied": 0,
                    "rejected": repair.rejected,
                }
                break
            repaired_validation = validate_draft(
                repair.draft,
                context,
                project=project,
                ruleset=ruleset,
                data_catalog=catalog,
                mock_semantic=mock_semantic,
                require_rules=True,
            )
            patch_errors = [
                issue.message for issue in repaired_validation.errors
            ]
            if patch_errors:
                result.warnings.append(
                    "修复草案未通过校验，停止修复："
                    + "；".join(patch_errors[:5])
                )
                break

            draft = repair.draft
            draft_path = run_dir.path / f"repair-{round_no}.draft.json"
            save_json_atomic(draft_path, draft.model_dump(mode="json"))
            store = load_case_set(store_path) or empty_case_set(
                project, context=context
            )
            rebuilt = build_proposal(
                project=project,
                draft_path=draft_path,
                draft=draft,
                context=context,
                validation=repaired_validation,
                store=store,
                store_path=store_path,
                ruleset=ruleset,
                data_catalog=catalog,
            )
            steps[f"repair_{round_no}"] = {
                "applied": repair.applied,
                "rejected": repair.rejected,
                "proposal": str(rebuilt.path) if rebuilt.path else None,
                "diff": rebuilt.diff.counts(),
            }
            if rebuilt.path is None:
                break
            if not config.approve_as:
                result.warnings.append(
                    f"修复草案待审：{rebuilt.path}；未提供 --approve-as，停止修复重跑"
                )
                break
            _fill_decisions(rebuilt.path / "cases.review.json", config.approve_as)
            approve_proposal(
                proposal_path=rebuilt.path,
                project=project,
                case_base=store_path,
                reviewer=config.approve_as,
            )
            store = load_case_set(store_path)
            if store is None:
                break
            cases = _scope_cases(store, draft)
            if not cases:
                break

            round_id = f"{run_id}-repair{round_no}"
            round_dir = RunDir(out_root, round_id)
            round_meta = {**run_meta, "run_id": round_id, "repair_round": round_no}
            report = _execute_round(
                project=project,
                config=config,
                run_dir=round_dir,
                run_meta=round_meta,
                impact=impact,
                profiles=context.get("profiles") or [],
                cases=cases,
                unmapped=unmapped,
                route_audit=route_audit,
                store=store,
                store_path=store_path,
                runtime=runtime,
                data_plan_path=data_plan_file,
            )
            result.report = report
            result.status = str(
                (report.get("gate") or {}).get("verdict") or "not_run"
            )
            steps[f"repair_{round_no}"]["run_dir"] = str(round_dir.path)
            steps[f"repair_{round_no}"]["verdict"] = result.status
            key_by_id = {case.id: case.case_key for case in cases}
            diagnosis, entries = _diagnose(
                run_dir=round_dir, cases=cases, llm=client, impact=impact
            )
            result.warnings.extend(diagnosis.warnings)
            if not diagnosis.eligible_case_ids:
                break

        return result
    finally:
        if sut is not None:
            sut.stop()
        if runtime is not None:
            runtime.stop()
        result.llm_usage = [usage.as_dict() for usage in client.usage]
        run_dir.write_json(
            "flow.json",
            {
                **result.as_dict(),
                "finished_at": utc_now(),
                "llm": {
                    "config": client.config.redacted(),
                    "usage": result.llm_usage,
                },
            },
        )
        if owned_llm:
            client.close()


__all__ = ["FlowConfig", "FlowResult", "run_flow"]
