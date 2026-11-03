"""M1 主流程：delta 全链路编排。

    路由清单 → git diff → 变更符号 → 影响接口 → 画像 → 用例
    → pytest 编码 → 收集自检 → 执行 → 确定性归因 → 有界重试 → 报告

设计约束：
- 每个阶段产物立即落盘，任一环节可独立重跑；
- 事实源环节（变更/路由/画像）失败即中止或显式标 unmapped，不降级猜测；
- 执行层零 LLM；重试只针对环境抖动，且次数有界；
- 所有"没做到"的范围进报告的 unmapped，不静默通过。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from contracts import DataPlanBundle, TestCase

from .artifacts import RunDir, new_run_id, utc_now
from .cases import generate_cases
from .cases.proposal import list_pending_proposals
from .cases.store import (
    approved_cases,
    case_summary,
    load_case_set,
    requirement_coverage,
    requirement_drift,
    resolve_case_base,
    store_hash,
)
from .changes.git_diff import GitDiffError, diff_files, repo_root, resolve_range
from .changes.impact import analyze_impact
from .changes.symbol_diff import symbols_for_changes
from .codegen import generate_pytest
from .execute.diagnose import classify
from .execute.runner import collect_only, run_pytest
from .inventory import (
    discover_openapi,
    inventory_routes,
    route_inventory_health,
)
from .persistence import sha256_json
from .mock import (
    MockPlanError,
    resolve_plan_path,
    start_mock_plan,
    summarize_evidence,
)
from .profile import build_profiles, load_spec, parse_operations
from .profile.openapi import normalize_path
from .report import build_report, render_markdown
from .rules import (
    RuleStoreError,
    load_rule_set,
    orphaned_rule_keys,
    rule_coverage,
    rule_set_hash,
    rule_set_snapshot,
)


@dataclass
class RunConfig:
    project: str
    mode: str = "delta"
    base: str = "main"
    target: str = "HEAD"
    base_url: str | None = None
    reset_url: str | None = None
    openapi: str | None = None
    out: str = ".astsight/runs"
    depth: int = 5
    max_cases_per_endpoint: int = 8
    skip_execute: bool = False
    allow_unmapped: bool = False
    retries: int = 1
    collect_only: bool = True
    codegraph: str = "auto"          # auto | on | off
    codegraph_module_root: str | None = None
    full_index: bool | None = None
    timeout: float = 600.0
    min_pass_rate: float = 0.95
    case_base: str | None = None
    fail_on_pending_proposals: bool = False
    min_requirement_coverage: float | None = None
    allow_stale_cases: bool = False
    data_plan: str | None = None
    database_url: str | None = None
    ruleset: str | None = None
    allow_stale_rules: bool = False
    mock_plan: str | None = None
    mock_admin_url: str | None = None
    mock_token: str | None = None
    mock_namespace: str | None = None
    mock_run_scope: str | None = None
    mock_drain_ms: int | None = None
    mock_ttl_ms: int | None = None
    mock_evidence: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _resolve_mock_evidence(
    project: Path, config: "RunConfig"
) -> tuple[str | None, str | None]:
    """定位 `mock_calls.jsonl`；未显式给出时向已启动的 Mock 服务查询。

    返回 `(path, reason)`。`reason` 只在“配置了 Mock 但证据不可得”时非空，
    用于报告降级；完全未配置 Mock 时两者均为 None。
    """
    if config.mock_evidence:
        raw = Path(config.mock_evidence)
        if not raw.is_absolute():
            cwd_candidate = (Path.cwd() / raw).resolve()
            raw = cwd_candidate if cwd_candidate.exists() else (project / raw).resolve()
        return str(raw), None
    if not config.mock_admin_url:
        return None, None
    headers = (
        {"Authorization": f"Bearer {config.mock_token}"}
        if config.mock_token else {}
    )
    try:
        response = httpx.get(
            f"{config.mock_admin_url.rstrip('/')}/_admin/server",
            headers=headers,
            timeout=5.0,
        )
        response.raise_for_status()
        history = (response.json() or {}).get("history_path")
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"mock_server_unreachable: {type(exc).__name__}: {exc}"[:500]
    if not history:
        return None, "mock_evidence_path_missing"
    return str(history), None


def _fetch_mock_sessions(
    config: "RunConfig", run_scope: str | None
) -> tuple[list[dict] | None, str | None]:
    """读取 Mock 服务的会话历史，用于 `mock_not_used` 检查。

    返回 `(sessions, reason)`。未配置 Mock 服务时返回 `(None, None)`。
    """
    if not config.mock_admin_url:
        return None, None
    headers = (
        {"Authorization": f"Bearer {config.mock_token}"}
        if config.mock_token else {}
    )
    params = {"run_scope": run_scope} if run_scope else {}
    try:
        response = httpx.get(
            f"{config.mock_admin_url.rstrip('/')}/_admin/sessions",
            headers=headers,
            params=params,
            timeout=5.0,
        )
        response.raise_for_status()
        payload = response.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"mock_sessions_unreachable: {type(exc).__name__}: {exc}"[:500]
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return None, "mock_sessions_invalid"
    return sessions, None


def _mock_env(
    config: "RunConfig", run_scope: str | None = None
) -> dict[str, Any]:
    return {
        "mock_admin_url": config.mock_admin_url,
        "mock_token": config.mock_token,
        "mock_namespace": config.mock_namespace,
        "mock_run_scope": run_scope or config.mock_run_scope,
        "mock_drain_ms": config.mock_drain_ms,
        "mock_ttl_ms": config.mock_ttl_ms,
    }


def _data_env(
    config: "RunConfig", data_plan_path: str | None
) -> dict[str, Any]:
    return {
        "data_plan_path": data_plan_path,
        "database_url": config.database_url,
    }


def _changed_scope_full(routes: list[dict]) -> tuple[list[dict], dict]:
    """full 模式：范围是全量接口，不伪造 git 变更。"""
    changed_apis = []
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


def _record_from_test(record: Any, case: TestCase | None, changed_endpoints: set[str]) -> dict:
    diagnosis = classify(record, case, changed_endpoints)
    status = "pass" if record.outcome == "passed" else ("skip" if record.outcome == "skipped" else "fail")
    return {
        "case_id": record.case_id,
        "endpoint": case.endpoint if case else "",
        "category": case.category if case else "",
        "nodeid": record.nodeid,
        "outcome": record.outcome,
        "message": record.message,
        "duration": record.duration,
        "attempts": [{"n": 1, "status": status, "reason": diagnosis.get("reason")}],
        "diagnosis": diagnosis,
    }


def _resolve_openapi_source(project: Path, raw: str | None) -> str | None:
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    path = Path(raw)
    if not path.is_absolute():
        cwd_candidate = (Path.cwd() / path).resolve()
        path = cwd_candidate if cwd_candidate.exists() else (project / path).resolve()
    return str(path.resolve())


def _load_data_plan_bundle(
    project: Path, config: "RunConfig"
) -> DataPlanBundle | None:
    """读取 C5b `data_plan.json`；非法计划直接失败，不降级成“无造数”。"""
    if not config.data_plan:
        return None
    raw = Path(config.data_plan)
    if not raw.is_absolute():
        cwd_candidate = (Path.cwd() / raw).resolve()
        raw = cwd_candidate if cwd_candidate.exists() else (project / raw).resolve()
    if not raw.is_file():
        raise FileNotFoundError(f"data_plan 不存在: {raw}")
    try:
        payload = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"data_plan 无法解析: {raw}: {exc}") from exc
    try:
        bundle = DataPlanBundle.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"data_plan 校验失败: {raw}: {exc}") from exc
    if bundle.rejected:
        details = "；".join(
            f"{item.case_key or item.case_id or '-'}:{item.reason}"
            for item in bundle.rejected[:20]
        )
        raise ValueError(f"data_plan 含被拒绝的用例，拒绝执行: {details}")
    return bundle


def _data_plan_state(
    bundle: DataPlanBundle | None,
    *,
    path: str | None,
    database_url: str | None,
    cases: list[Any],
) -> dict[str, Any] | None:
    if bundle is None:
        return None
    case_ids = {case.id for case in cases}
    case_keys = {
        getattr(case, "case_key", "") for case in cases if getattr(case, "case_key", "")
    }
    matched = [
        plan
        for plan in bundle.plans
        if (plan.case_id and plan.case_id in case_ids)
        or (plan.case_key and plan.case_key in case_keys)
    ]
    missing = [
        case.id
        for case in cases
        if case.preconditions
        and not any(
            (plan.case_id == case.id)
            or (plan.case_key and plan.case_key == getattr(case, "case_key", ""))
            for plan in matched
        )
    ]
    return {
        "path": path,
        "run_scope": bundle.run_scope,
        "plans": len(matched),
        "database_url_configured": bool(database_url),
        "missing_plans": missing,
        "warnings": list(bundle.warnings),
    }


def _rules_state(
    project: Path,
    config: "RunConfig",
    *,
    store_snapshot: dict[str, Any] | None,
    cases: list[Any],
) -> dict[str, Any] | None:
    """规则库与覆盖快照；未配置且项目无 ruleset.json 时返回 None。"""
    raw = config.ruleset
    explicit = raw is not None
    path = Path(raw) if raw else project / "ruleset.json"
    if not path.is_absolute():
        path = (project / path).resolve()
    else:
        path = path.resolve()
    if not path.is_file():
        if not explicit:
            return None
        return {
            "checked": False,
            "reason": "ruleset_not_found",
            "path": str(path),
            "gate_blockers": ["规则库不可加载：文件不存在"],
            "gate_warnings": [],
        }
    try:
        ruleset = load_rule_set(path)
    except RuleStoreError as exc:
        return {
            "checked": False,
            "reason": "ruleset_invalid",
            "path": str(path),
            "error": str(exc)[:2_000],
            "gate_blockers": ["规则库不可加载或校验失败"],
            "gate_warnings": [],
        }

    coverage = rule_coverage(ruleset, cases).as_dict()
    refs = [
        ref.rule_key
        for case in cases
        for ref in (getattr(case, "rule_refs", None) or [])
    ]
    orphaned = orphaned_rule_keys(refs, ruleset)
    active = {"active"}
    stale_rule_cases = [
        {
            "case_id": getattr(case, "id", None),
            "case_key": getattr(case, "case_key", ""),
            "rule_keys": [
                ref.rule_key
                for ref in (getattr(case, "rule_refs", None) or [])
                if ref.rule_key in {rule.rule_key for rule in ruleset.all_rules()
                                    if rule.status.value not in active}
            ],
        }
        for case in cases
    ]
    stale_rule_cases = [item for item in stale_rule_cases if item["rule_keys"]]
    snapshot = dict(store_snapshot or {})
    current_snapshot = rule_set_snapshot(ruleset)
    drift_changed = bool(
        snapshot
        and snapshot.get("hash")
        and snapshot.get("hash") != current_snapshot.get("hash")
    )
    conflicts = [conflict.model_dump(mode="json") for conflict in ruleset.conflicts]
    blockers: list[str] = []
    warnings: list[str] = []
    if conflicts:
        # 规则阶段没有人工裁决，冲突在用例审核时确认；这里只提示，不阻断执行。
        warnings.append(f"规则库记录了 {len(conflicts)} 个待确认冲突")
    if orphaned:
        warnings.append(
            f"{len(orphaned)} 个用例引用的规则已不在规则库中（rule_orphaned）"
        )
    if stale_rule_cases:
        warnings.append(
            f"{len(stale_rule_cases)} 条用例引用非 active 规则（drift/ungrounded）"
        )
    if drift_changed:
        message = "规则库 hash 与用例库快照不一致，已批准用例可能过期"
        if config.allow_stale_rules:
            warnings.append(message + "（--allow-stale-rules）")
        else:
            blockers.append(message)
    if coverage["business"]["stats"]["uncovered"] or coverage["contract"]["stats"]["uncovered"]:
        warnings.append(
            "存在未被用例覆盖的规则："
            f"BR {coverage['business']['stats']['uncovered']} / "
            f"CR {coverage['contract']['stats']['uncovered']}"
        )
    return {
        "checked": True,
        "path": str(path),
        "ruleset_id": ruleset.ruleset_id,
        "version": ruleset.version,
        "hash": rule_set_hash(ruleset),
        "snapshot": current_snapshot,
        "store_snapshot": snapshot,
        "counts": {
            "business_rules": len(ruleset.rules),
            "contract_rules": len(ruleset.contract_rules),
        },
        "coverage": coverage,
        "questions": [item.model_dump(mode="json") for item in ruleset.questions],
        "gaps": [item.model_dump(mode="json") for item in ruleset.gaps],
        "conflicts": [
            item.model_dump(mode="json") for item in ruleset.conflicts
        ],
        "orphaned_rule_keys": orphaned,
        "stale_rule_cases": stale_rule_cases,
        "rule_drift": {
            "changed": drift_changed,
            "store_hash": snapshot.get("hash"),
            "current_hash": current_snapshot.get("hash"),
        },
        "gate_blockers": blockers,
        "gate_warnings": warnings,
    }


def _case_contract_drift(
    project: Path, config: RunConfig, store: Any
) -> dict[str, Any]:
    if store is None:
        return {"checked": False, "changed": False, "reason": "no_case_store",
                "stale_cases": [], "missing_endpoints": []}
    raw = config.openapi or (store.openapi or {}).get("source")
    source = discover_openapi(
        project, _resolve_openapi_source(project, raw), config.base_url
    )
    if not source:
        return {
            "checked": False,
            "changed": False,
            "reason": "openapi_not_found",
            "stale_cases": [],
            "missing_endpoints": [],
        }
    try:
        spec = load_spec(source)
        operations = parse_operations(spec)
    except Exception as exc:
        return {
            "checked": False,
            "changed": False,
            "reason": f"openapi_error: {type(exc).__name__}: {exc}"[:500],
            "stale_cases": [],
            "missing_endpoints": [],
        }
    current_hash = sha256_json(spec)
    stored_hash = str((store.openapi or {}).get("sha256") or "")
    changed = bool(stored_hash and stored_hash != current_hash)
    index = {
        (op.method.upper(), normalize_path(op.path)) for op in operations
    }
    stale: list[dict[str, str]] = []
    for entry in approved_cases(store):
        parts = entry.endpoint.split(None, 1)
        if len(parts) != 2:
            stale.append({"case_id": entry.id, "endpoint": entry.endpoint,
                          "reason": "invalid_endpoint"})
            continue
        method, path = parts
        if (method.upper(), normalize_path(path)) not in index:
            stale.append({"case_id": entry.id, "endpoint": entry.endpoint,
                          "reason": "endpoint_missing"})
    return {
        "checked": True,
        "changed": changed,
        "current_sha256": current_hash,
        "stored_sha256": stored_hash,
        "source": source,
        "operation_count": len(operations),
        "stale_cases": stale,
        "missing_endpoints": [item["endpoint"] for item in stale],
    }


def build_case_store_state(
    *,
    project: Path,
    config: RunConfig,
    store: Any,
    store_path: Path,
    pending: list[dict[str, Any]],
    contract_drift: dict[str, Any],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    approved = approved_cases(store) if store is not None else []
    if not approved:
        blockers.append("没有已审核通过的用例（caseset.json 中 approved=0）")
    if pending:
        message = (
            f"存在 {len(pending)} 个待审 proposal；本次运行使用上次批准的 store 版本"
        )
        if config.fail_on_pending_proposals:
            blockers.append(message + "（--fail-on-pending-proposals）")
        else:
            warnings.append(message)
    if not contract_drift.get("checked"):
        warnings.append(
            "contract drift 未校验："
            + str(contract_drift.get("reason") or "openapi_not_found")
        )
    elif contract_drift.get("changed"):
        warnings.append("OpenAPI 契约已变化，已批准用例可能过期")
    stale = contract_drift.get("stale_cases") or []
    if stale:
        message = (
            f"{len(stale)} 条已批准用例的 endpoint 在 OpenAPI 中已不存在："
            + ", ".join(item.get("endpoint", "") for item in stale[:8])
        )
        if config.allow_stale_cases:
            warnings.append(message + "（--allow-stale-cases）")
        else:
            blockers.append(message)
    threshold = config.min_requirement_coverage
    if threshold is not None and float(coverage.get("rate") or 0.0) < threshold:
        blockers.append(
            f"需求覆盖率 {float(coverage.get('rate') or 0.0):.2%} 低于 {threshold:.0%}"
        )
    summary = case_summary(store) if store is not None else {
        "approved": 0, "deprecated": 0, "history_versions": 0,
        "total_cases": 0, "review_events": 0,
    }
    if summary.get("deprecated"):
        warnings.append(f"用例库存在 {summary['deprecated']} 条 deprecated 用例")
    drift = (
        requirement_drift(store, project)
        if store is not None
        else {"checked": False, "changed": False, "changed_docs": [], "missing_docs": []}
    )
    if drift.get("changed_docs"):
        warnings.append(
            f"{len(drift['changed_docs'])} 份需求文档已变化，store 中的引用可能过期"
        )
    if drift.get("missing_docs"):
        warnings.append(
            f"{len(drift['missing_docs'])} 份需求文档缺失：" + ", ".join(drift["missing_docs"][:8])
        )
    degraded = bool(
        pending
        or contract_drift.get("changed")
        or stale
        or not contract_drift.get("checked")
        or drift.get("changed_docs")
        or drift.get("missing_docs")
        or summary.get("deprecated")
    )
    return {
        "case_base": str(store_path),
        "store_version": store.store_version if store is not None else 0,
        "store_hash": store_hash(store) if store is not None else "",
        "approved_at": store.approved_at if store is not None else None,
        "approved_by": store.approved_by if store is not None else None,
        "ruleset": dict(store.ruleset) if store is not None else {},
        "summary": summary,
        "pending_proposals": len(pending),
        "pending_proposal_paths": [item.get("path", "") for item in pending],
        "degraded": degraded,
        "contract_drift": contract_drift,
        "requirement_drift": drift,
        "requirement_coverage": coverage,
        "review_log_tail": (
            [event.model_dump(mode="json") for event in store.review_log[-10:]]
            if store is not None else []
        ),
        "gate_blockers": blockers,
        "gate_warnings": warnings,
    }


def _execute_and_report_inner(
    *,
    project: Path,
    config: RunConfig,
    run_dir: RunDir,
    run_meta: dict,
    changes: dict,
    impact: dict,
    profiles: list[dict],
    cases: list[Any],
    unmapped: dict,
    route_audit: dict,
    codegen_gate: dict | None = None,
    case_store_state: dict | None = None,
    route_notes: list[str] | None = None,
    mock_plan_state: dict | None = None,
) -> dict:
    # 未显式指定 run_scope 时用 run_id：既让响应变量按本次运行隔离，
    # 也让报告能只扫描本次运行产生的 Mock 会话。
    mock_run_scope = config.mock_run_scope or str(run_meta.get("run_id") or "") or None
    if config.mock_admin_url:
        # 不落 token：报告只记录定位信息与指纹由 MockServer 自己提供。
        run_meta.setdefault(
            "mock",
            {
                "admin_url": config.mock_admin_url,
                "namespace": config.mock_namespace,
                "run_scope": mock_run_scope,
                "drain_ms": config.mock_drain_ms,
                "ttl_ms": config.mock_ttl_ms,
            },
        )
    if mock_plan_state:
        run_meta.setdefault("mock", {})
        run_meta["mock"]["plan"] = mock_plan_state.get("plan")
        run_meta["mock"]["plan_mode"] = mock_plan_state.get("mode")
        run_meta["mock"]["plan_routes"] = mock_plan_state.get("route_count")
        run_meta["mock"]["plan_injections"] = mock_plan_state.get("injection_count")
    run_dir.write_json("run_meta.json", run_meta)
    run_dir.write_json("changed_apis.json", impact)
    run_dir.write_json("cases.json", [case.model_dump(mode="json") for case in cases])

    data_bundle = _load_data_plan_bundle(project, config)
    data_plan_state = _data_plan_state(
        data_bundle,
        path=config.data_plan,
        database_url=config.database_url,
        cases=cases,
    )
    if data_plan_state is not None:
        run_dir.write_json("data_plan_state.json", data_plan_state)
    if case_store_state is not None:
        case_store_state = {**case_store_state, "data_plan": data_plan_state}

    rules_state = _rules_state(
        project,
        config,
        store_snapshot=(case_store_state or {}).get("ruleset"),
        cases=cases,
    )

    generated_dir = run_dir.path / "generated"
    data_plan_path: str | None = None
    codegen_dict: dict = {}
    if cases:
        codegen = generate_pytest(cases, generated_dir, data_bundle=data_bundle)
        codegen_dict = codegen.as_dict()
        run_dir.write_json("codegen.json", codegen_dict)
        if data_bundle is not None:
            plan_file = generated_dir / "data_plan.json"
            data_plan_path = str(plan_file.resolve())
            if plan_file.is_file():
                run_dir.write_json(
                    "data_plan.json",
                    json.loads(plan_file.read_text(encoding="utf-8")),
                )

    entries: list[dict] = []
    cases_by_id = {case.id: case for case in cases}
    changed_endpoints = {api["handler"] for api in impact.get("changed_apis", [])}
    execution_meta: dict[str, Any] = {"collect_only": False, "executed": False}
    if cases and not config.skip_execute and config.base_url:
        if config.collect_only:
            ok, output = collect_only(
                generated_dir,
                base_url=config.base_url,
                reset_url=config.reset_url,
                timeout=config.timeout,
                **_mock_env(config, mock_run_scope),
                **_data_env(config, data_plan_path),
            )
            execution_meta["collect_only"] = True
            execution_meta["collect_ok"] = ok
            if not ok:
                codegen_dict["collection_error"] = output[-4000:]
                execution_meta["collection_output"] = output[-4000:]
        if execution_meta.get("collect_ok", True):
            first = run_pytest(
                generated_dir,
                base_url=config.base_url,
                reset_url=config.reset_url,
                junit_path=run_dir.path / "results.xml",
                timeout=config.timeout,
                **_mock_env(config, mock_run_scope),
                **_data_env(config, data_plan_path),
            )
            run_dir.write_json("execution-first.json", first.as_dict())
            execution_meta["executed"] = True
            execution_meta["returncode"] = first.returncode
            execution_meta["collection_errors"] = first.collection_errors
            entries = [
                _record_from_test(record, cases_by_id.get(record.case_id), changed_endpoints)
                for record in first.records
            ]
            attempt = 2
            while attempt <= 1 + max(0, config.retries):
                if not _apply_retry(
                    entries, cases_by_id, generated_dir, config, run_dir,
                    changed_endpoints, attempt_no=attempt,
                    mock_run_scope=mock_run_scope,
                    data_plan_path=data_plan_path,
                ):
                    break
                attempt += 1
    execution_meta["data_plan"] = data_plan_state
    run_dir.write_json("execution.json", {"meta": execution_meta, "records": entries})

    mock_evidence_path, mock_evidence_reason = _resolve_mock_evidence(project, config)
    mock_sessions, mock_sessions_reason = _fetch_mock_sessions(config, mock_run_scope)
    mock_summary = summarize_evidence(
        mock_evidence_path,
        sessions=mock_sessions,
        allow_missing=bool(mock_plan_state),
    )
    if mock_evidence_reason:
        mock_summary = {
            **mock_summary,
            "checked": False,
            "reason": mock_evidence_reason,
            "degraded": True,
        }
    if mock_sessions_reason:
        mock_summary = {
            **mock_summary,
            "sessions_checked": False,
            "sessions_reason": mock_sessions_reason,
            "degraded": True,
        }
    elif mock_summary.get("checked") and not mock_summary.get("sessions_checked"):
        # 只有调用证据、没有会话快照时无法证明绑定 dep 真的被调用。
        mock_summary = {
            **mock_summary,
            "sessions_reason": "mock_admin_url_not_configured",
            "degraded": True,
        }
    if mock_plan_state:
        mock_summary = {**mock_summary, "plan": mock_plan_state}
    run_dir.write_json("mock_evidence.json", mock_summary)

    report = build_report(
        run_meta={**run_meta, "finished_at": utc_now()},
        changes=changes,
        impact=impact,
        profiles=profiles,
        cases=cases,
        records=entries,
        unmapped=unmapped,
        route_audit=route_audit,
        codegen=codegen_dict,
        gate_config={
            "allow_unmapped": config.allow_unmapped,
            "min_pass_rate": config.min_pass_rate,
            "min_requirement_coverage": config.min_requirement_coverage,
            **(codegen_gate or {}),
        },
        case_store=case_store_state,
        mock_evidence=mock_summary,
        data_plan=data_plan_state,
        rules=rules_state,
    )
    report["execution"] = execution_meta
    if route_notes is not None:
        report["route_notes"] = route_notes
    report["artifacts"] = _artifacts(run_dir)
    report_path = run_dir.write_json("report.json", report)
    markdown = render_markdown(report)
    md_path = run_dir.write_text("report.md", markdown)
    report["artifacts"] = {
        **_artifacts(run_dir),
        "report_json": run_dir.rel(report_path),
        "report_md": run_dir.rel(md_path),
    }
    run_dir.write_json("report.json", report)
    return report


def _execute_and_report(
    *,
    project: Path,
    config: RunConfig,
    run_dir: RunDir,
    run_meta: dict,
    changes: dict,
    impact: dict,
    profiles: list[dict],
    cases: list[Any],
    unmapped: dict,
    route_audit: dict,
    codegen_gate: dict | None = None,
    case_store_state: dict | None = None,
    route_notes: list[str] | None = None,
) -> dict:
    """执行链路入口；`--mock-plan` 时先启动内嵌服务，结束后必定停止。"""
    if not config.mock_plan:
        return _execute_and_report_inner(
            project=project,
            config=config,
            run_dir=run_dir,
            run_meta=run_meta,
            changes=changes,
            impact=impact,
            profiles=profiles,
            cases=cases,
            unmapped=unmapped,
            route_audit=route_audit,
            codegen_gate=codegen_gate,
            case_store_state=case_store_state,
            route_notes=route_notes,
        )

    conflicts = [
        flag
        for flag, value in (
            ("--mock-admin-url", config.mock_admin_url),
            ("--mock-token", config.mock_token),
            ("--mock-namespace", config.mock_namespace),
            ("--mock-evidence", config.mock_evidence),
        )
        if value
    ]
    if conflicts:
        raise ValueError(
            "--mock-plan 不能与 "
            + " / ".join(conflicts)
            + " 同时使用：内嵌服务会生成自己的地址、token、namespace 和证据路径"
        )

    plan_path = resolve_plan_path(project, config.mock_plan)
    try:
        runtime = start_mock_plan(
            plan_path,
            history_path=run_dir.path / "mock_calls.jsonl",
        )
    except MockPlanError as exc:
        raise MockPlanError(exc.code, f"{exc.detail}（plan: {plan_path}）") from exc

    run_id = str(run_meta.get("run_id") or "")
    try:
        plan_state = runtime.report_state()
        run_dir.write_json("run_manifest.json", runtime.manifest(run_id=run_id))
        run_dir.write_json("mock_injection.json", runtime.injection_document())
        effective = replace(
            config,
            mock_admin_url=runtime.server.admin_endpoint,
            mock_token=runtime.token,
            mock_namespace=runtime.plan.namespace,
            mock_evidence=(
                str(runtime.evidence_path) if runtime.evidence_path else None
            ),
        )
        return _execute_and_report_inner(
            project=project,
            config=effective,
            run_dir=run_dir,
            run_meta=run_meta,
            changes=changes,
            impact=impact,
            profiles=profiles,
            cases=cases,
            unmapped=unmapped,
            route_audit=route_audit,
            codegen_gate=codegen_gate,
            case_store_state=case_store_state,
            route_notes=route_notes,
            mock_plan_state=plan_state,
        )
    finally:
        runtime.stop()


def execute_cases(
    *,
    project: Path,
    config: RunConfig,
    run_dir: RunDir,
    run_meta: dict,
    changes: dict,
    impact: dict,
    profiles: list[dict],
    cases: list[Any],
    unmapped: dict,
    route_audit: dict,
    codegen_gate: dict | None = None,
    case_store_state: dict | None = None,
    route_notes: list[str] | None = None,
) -> dict:
    """不启动新 Mock 的执行入口（Mock 可已由调用方启动）。"""
    return _execute_and_report(
        project=project,
        config=config,
        run_dir=run_dir,
        run_meta=run_meta,
        changes=changes,
        impact=impact,
        profiles=profiles,
        cases=cases,
        unmapped=unmapped,
        route_audit=route_audit,
        codegen_gate=codegen_gate,
        case_store_state=case_store_state,
        route_notes=route_notes,
    )


def execute_with_runtime(
    *,
    project: Path,
    config: RunConfig,
    run_dir: RunDir,
    run_meta: dict,
    changes: dict,
    impact: dict,
    profiles: list[dict],
    cases: list[Any],
    unmapped: dict,
    route_audit: dict,
    runtime: Any,
    codegen_gate: dict | None = None,
    case_store_state: dict | None = None,
    route_notes: list[str] | None = None,
) -> dict:
    """使用调用方已启动的 Mock runtime；不负责停止它。

    Agent 流程需要先启动 Mock 计算注入 URL，再用这些环境变量启动 SUT，
    因此 Mock 的生命周期必须由外层编排器掌握。
    """
    run_id = str(run_meta.get("run_id") or "")
    plan_state = runtime.report_state()
    run_dir.write_json("run_manifest.json", runtime.manifest(run_id=run_id))
    run_dir.write_json("mock_injection.json", runtime.injection_document())
    effective = replace(
        config,
        mock_admin_url=runtime.server.admin_endpoint,
        mock_token=runtime.token,
        mock_namespace=runtime.plan.namespace,
        mock_evidence=(
            str(runtime.evidence_path) if runtime.evidence_path else None
        ),
    )
    return _execute_and_report_inner(
        project=project,
        config=effective,
        run_dir=run_dir,
        run_meta=run_meta,
        changes=changes,
        impact=impact,
        profiles=profiles,
        cases=cases,
        unmapped=unmapped,
        route_audit=route_audit,
        codegen_gate=codegen_gate,
        case_store_state=case_store_state,
        route_notes=route_notes,
        mock_plan_state=plan_state,
    )


def _run_cases_mode(
    config: RunConfig, project: Path, run_dir: RunDir, run_id: str
) -> dict:
    store_path = resolve_case_base(project, config.case_base)
    store = load_case_set(store_path)
    pending = list_pending_proposals(project)
    coverage = (
        requirement_coverage(store)
        if store is not None
        else {"total": 0, "covered": 0, "rate": 0.0, "uncovered": []}
    )
    contract_drift = _case_contract_drift(project, config, store)
    state = build_case_store_state(
        project=project,
        config=config,
        store=store,
        store_path=store_path,
        pending=pending,
        contract_drift=contract_drift,
        coverage=coverage,
    )
    cases = approved_cases(store) if store is not None else []
    run_meta = {
        "run_id": run_id,
        "mode": "cases",
        "project": str(project),
        "started_at": utc_now(),
        "base": None,
        "target": None,
        "base_url": config.base_url,
        "openapi": config.openapi,
        "case_base": str(store_path),
        "store_version": store.store_version if store is not None else 0,
        **config.extra,
    }
    impact: dict[str, Any] = {
        "changed_apis": [],
        "unresolved_symbols": [],
        "traces": [],
        "changed_symbols": [],
        "unsupported_files": [],
        "scope": "cases",
        "note": "cases 模式只使用 caseset.json 中已审核通过的用例",
    }
    seen: set[str] = set()
    for entry in cases:
        if entry.endpoint in seen:
            continue
        seen.add(entry.endpoint)
        method, _, path = entry.endpoint.partition(" ")
        impact["changed_apis"].append(
            {
                "handler": entry.endpoint,
                "method": method,
                "path": path,
                "route_handler": "",
                "confidence": "high",
                "evidence": "verified",
                "changed_symbols": [],
                "change_files": [],
                "trace": [],
                "route_source": "caseset",
                "scope": "cases",
            }
        )
    run_dir.write_json(
        "caseset.json",
        store.model_dump(mode="json") if store is not None else {},
    )
    run_dir.write_json("requirement_coverage.json", coverage)
    run_dir.write_json("pending_proposals.json", pending)
    return _execute_and_report(
        project=project,
        config=config,
        run_dir=run_dir,
        run_meta=run_meta,
        changes={
            "mode": "cases",
            "case_base": str(store_path),
            "store_version": store.store_version if store is not None else 0,
            "pending_proposals": len(pending),
        },
        impact=impact,
        profiles=[],
        cases=cases,
        unmapped={
            "symbols": [],
            "routes": [],
            "profile_unmapped": [],
            "unsupported_files": [],
            "case_store_missing_endpoints": contract_drift.get("missing_endpoints") or [],
        },
        route_audit={
            "source": "caseset",
            "routes": [],
            "note": "cases 模式不扫描路由/CodeGraph",
        },
        case_store_state=state,
    )


def _apply_retry(
    entries: list[dict],
    cases_by_id: dict[str, TestCase],
    generated_dir: Path,
    config: RunConfig,
    run_dir: RunDir,
    changed_endpoints: set[str],
    *,
    attempt_no: int,
    mock_run_scope: str | None = None,
    data_plan_path: str | None = None,
) -> bool:
    """对可重试失败做一次有界重跑。返回是否有重跑。"""
    retryable = [
        e for e in entries
        if e["outcome"] in ("failed", "error")
        and (e.get("diagnosis") or {}).get("retryable")
    ]
    if not retryable:
        return False

    nodeids = [e["nodeid"] for e in retryable if e.get("nodeid")]
    if not nodeids:
        return False

    retry_result = run_pytest(
        generated_dir,
        base_url=config.base_url,
        reset_url=config.reset_url,
        junit_path=run_dir.path / f"results-attempt{attempt_no}.xml",
        nodeids=nodeids,
        timeout=config.timeout,
        **_mock_env(config, mock_run_scope),
        **_data_env(config, data_plan_path),
    )
    run_dir.write_json(f"attempt{attempt_no}_summary.json", retry_result.as_dict())
    retry_by_node = {r.nodeid: r for r in retry_result.records}

    for entry in retryable:
        retried = retry_by_node.get(entry["nodeid"])
        if retried is None:
            continue
        case = cases_by_id.get(entry["case_id"])
        diagnosis = classify(retried, case, changed_endpoints)
        status = "pass" if retried.outcome == "passed" else "fail"
        entry["attempts"].append(
            {"n": attempt_no, "status": status, "reason": diagnosis.get("reason")}
        )
        entry["outcome"] = retried.outcome
        entry["message"] = retried.message
        entry["duration"] = retried.duration
        entry["diagnosis"] = diagnosis
    return True


def run(config: RunConfig) -> dict:
    project = Path(config.project).resolve()
    if not project.exists():
        raise FileNotFoundError(f"项目路径不存在: {project}")

    run_id = new_run_id(config.mode)
    run_dir = RunDir(config.out if Path(config.out).is_absolute() else project / config.out, run_id)

    if config.mode == "cases":
        return _run_cases_mode(config, project, run_dir, run_id)

    run_meta: dict[str, Any] = {
        "run_id": run_id,
        "mode": config.mode,
        "project": str(project),
        "started_at": utc_now(),
        "base": config.base if config.mode == "delta" else None,
        "target": config.target if config.mode == "delta" else None,
        "base_url": config.base_url,
        "openapi": config.openapi,
        **config.extra,
    }
    run_dir.write_json("run_meta.json", run_meta)

    # ── 1. 路由清单（C3 输入）──────────────────────────────────
    # 路由清单 = 源码可归因路由 ∪ OpenAPI operation（后者只用于清单与 operationId 兜底）。
    # 只在本地契约文件上做补充；远端 URL 留给后续画像阶段，避免重复网络请求。
    openapi_source_hint = discover_openapi(project, config.openapi, None)
    routes, route_audit, graph_client, graph_notes = inventory_routes(
        project,
        codegraph=config.codegraph,
        codegraph_module_root=config.codegraph_module_root,
        full_index=config.full_index,
        openapi_source=openapi_source_hint,
    )
    # 路由清单健康度：少数派写法（生成接口/函数式路由/Spring Data REST）会让
    # 可归因路由远少于真实接口。这里不猜归因，只让“少报”变成显式告警。
    supplement = (route_audit or {}).get("openapi_supplement") or {}
    route_health = route_inventory_health(
        project, routes, openapi_operations=supplement.get("operations")
    )
    route_audit = {**(route_audit or {}), "health": route_health}
    run_dir.write_json("routes.json", {"routes": routes, "audit": route_audit})
    if not routes:
        hint = ""
        if route_health.get("controller_files"):
            hint = (
                f"检测到 {route_health['controller_files']} 个 Controller 文件但未提取到路由，"
                "路由可能声明在生成/父接口或函数式路由中。"
            )
        raise RuntimeError(
            "未发现任何路由。" + hint
            + "Flask/Django/FastAPI 由框架插件扫描，Java/Spring 由 CodeGraph 提取；"
            "请确认项目路径、CodeGraph 配置，或提供 OpenAPI。不降级猜测路由。"
        )

    # ── 2. 变更感知（C2）───────────────────────────────────────
    unsupported_files: list[dict] = []
    changes_meta: dict[str, Any] = {}
    impact_dict: dict[str, Any]

    if config.mode == "delta":
        root = repo_root(project)
        if root != project:
            # git diff 路径相对仓库根；M1 明确要求 project 为仓库根，避免静默错位
            raise GitDiffError(
                f"delta 模式要求 --project 指向 git 仓库根，当前为子目录: {project}（根: {root}）"
            )
        range_arg, comparison = resolve_range(project, config.base, config.target)
        file_changes, changes_meta = diff_files(project, config.base, config.target)
        changes_meta["comparison"] = comparison
        changes_meta["range"] = range_arg
        run_meta["comparison"] = comparison
        run_meta["range"] = range_arg
        changed_symbols, unsupported_files = symbols_for_changes(
            project,
            config.base,
            config.target,
            file_changes,
            codegraph_client=graph_client,
        )
        run_dir.write_json(
            "changes.json",
            {
                **changes_meta,
                "files": [c.as_dict() for c in file_changes],
                "symbols": [s.as_dict() for s in changed_symbols],
                "unsupported_files": unsupported_files,
            },
        )
        summary = analyze_impact(
            changed_symbols,
            routes,
            graph_client,
            depth=config.depth,
            project_root=project,
        )
        impact_dict = summary.as_dict()
        impact_dict["changed_symbols"] = [s.as_dict() for s in changed_symbols]
        impact_dict["unsupported_files"] = unsupported_files
    else:
        run_meta["comparison"] = "full"
        changed_apis, changes_meta = _changed_scope_full(routes)
        impact_dict = {
            "changed_apis": changed_apis,
            "unresolved_symbols": [],
            "traces": [],
            "changed_symbols": [],
            "unsupported_files": [],
            "scope": "full",
        }
        run_dir.write_json("changes.json", changes_meta)

    run_dir.write_json("changed_apis.json", impact_dict)
    run_dir.write_json("run_meta.json", run_meta)

    if not impact_dict["changed_apis"]:
        # 不影响任何接口也是合法结论，但必须能说清"为什么没有"
        run_meta["note"] = "本次变更未命中任何接口；unresolved 明细见 changed_apis.json"
        report = _finish_without_cases(
            run_dir, run_meta, changes_meta, impact_dict, route_audit,
            unsupported_files, [], config,
        )
        return report

    # ── 3. OpenAPI + 画像（C3）────────────────────────────────
    openapi_source = discover_openapi(project, config.openapi, config.base_url)
    spec = load_spec(openapi_source) if openapi_source else {}
    operations = parse_operations(spec) if spec else []
    run_meta["openapi_source"] = openapi_source
    run_meta["openapi_operations"] = len(operations)

    # 契约路径是请求地址的最终依据。路由插件与 OpenAPI 的参数写法可能不同
    # （如 /orders/{id:int} vs /orders/{id}），先对齐影响结果，避免后续
    # "变更接口"与"已生成用例"用两套路径名互相判缺口。
    operation_index = {
        (op.method.upper(), normalize_path(op.path)): op for op in operations
    }
    aligned = False
    for api in impact_dict["changed_apis"]:
        operation = operation_index.get(
            ((api.get("method") or "").upper(), normalize_path(api.get("path", "")))
        )
        if operation is not None and operation.path != api.get("path"):
            api["path_aligned_from"] = api.get("path", "")
            api["path"] = operation.path
            api["handler"] = f"{api.get('method', '').upper()} {operation.path}"
            aligned = True
    if aligned:
        run_dir.write_json("changed_apis.json", impact_dict)

    profile_routes = []
    for api in impact_dict["changed_apis"]:
        profile_routes.append(
            {
                "method": api.get("method", ""),
                "path": api.get("path", ""),
                "handler": api.get("route_handler", ""),
                "owner": api.get("route_owner", ""),
                "file": api.get("route_file") or (api.get("change_files") or [""])[0],
                "source": api.get("route_source", ""),
                "confidence": api.get("evidence", "inferred"),
            }
        )
    profiles, profile_unmapped = build_profiles(
        profile_routes,
        operations,
        codegraph_client=graph_client,
        project_root=project,
    )
    run_dir.write_json("profiles.json", {"profiles": profiles, "unmapped": profile_unmapped})

    # ── 4. 模板用例（C4）────────────────────────────────────
    cases: list[TestCase] = generate_cases(
        profiles, max_per_endpoint=config.max_cases_per_endpoint
    )

    # ── 5. 编码 / 执行 / 报告（C6/C7/C7.4）──────────────────
    profile_unmapped_routes = [
        {
            "endpoint": item.get("endpoint", ""),
            "handler": item.get("handler", ""),
            "reason": item.get("reason", ""),
            "file": item.get("file", ""),
        }
        for item in profile_unmapped
    ]
    route_conflicts = [
        route for route in routes if route.get("confidence") == "unmapped"
    ]
    unmapped = {
        "symbols": impact_dict.get("unresolved_symbols", []),
        "routes": route_conflicts,
        "profile_unmapped": profile_unmapped_routes,
        "unsupported_files": unsupported_files,
    }
    return _execute_and_report(
        project=project,
        config=config,
        run_dir=run_dir,
        run_meta=run_meta,
        changes=changes_meta,
        impact=impact_dict,
        profiles=profiles,
        cases=cases,
        unmapped=unmapped,
        route_audit=route_audit,
        route_notes=graph_notes,
    )


def _finish_without_cases(
    run_dir: RunDir,
    run_meta: dict,
    changes: dict,
    impact: dict,
    route_audit: dict,
    unsupported: list[dict],
    profiles: list,
    config: RunConfig,
) -> dict:
    run_dir.write_json("run_meta.json", run_meta)
    report = build_report(
        run_meta={**run_meta, "finished_at": utc_now()},
        changes=changes,
        impact=impact,
        profiles=profiles,
        cases=[],
        records=[],
        unmapped={
            "symbols": impact.get("unresolved_symbols", []),
            "routes": [],
            "unsupported_files": unsupported,
        },
        route_audit=route_audit,
        codegen={},
        gate_config={"allow_unmapped": config.allow_unmapped},
    )
    report["artifacts"] = _artifacts(run_dir)
    report_path = run_dir.write_json("report.json", report)
    report["artifacts"]["report_json"] = run_dir.rel(report_path)
    markdown = render_markdown(report)
    md_path = run_dir.write_text("report.md", markdown)
    report["artifacts"]["report_md"] = run_dir.rel(md_path)
    run_dir.write_json("report.json", report)
    return report


def _artifacts(run_dir: RunDir) -> dict:
    names = [
        "run_meta.json", "routes.json", "changes.json", "changed_apis.json",
        "profiles.json", "caseset.json", "requirement_coverage.json",
        "pending_proposals.json", "cases.json", "codegen.json", "execution.json",
        "data_plan.json", "data_plan_state.json",
        "run_manifest.json", "mock_injection.json", "mock_evidence.json",
        "results.xml", "report.json", "report.md",
    ]
    return {name: run_dir.rel(run_dir.path / name) for name in names}
