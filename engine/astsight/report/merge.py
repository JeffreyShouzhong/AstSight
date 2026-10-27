"""C7.4 报告合并。

门禁口径（对齐设计文档 C7.4）：
- **只认真实缺陷**（`code_defect`）与覆盖边界（`unmapped`），不认“重试后通过率”；
- 未执行/未生成的接口单列，不得计入通过率分母；
- 重试恢复单列 `recovered`，作为稳定性信号，不是质量结论。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def build_report(
    *,
    run_meta: dict,
    changes: dict,
    impact: dict,
    profiles: list[dict],
    cases: list[Any],
    records: list[dict],
    unmapped: dict,
    route_audit: dict,
    codegen: dict,
    gate_config: dict | None = None,
    case_store: dict | None = None,
) -> dict:
    gate_config = gate_config or {}
    cases_by_id = {c.id: c for c in cases}

    by_endpoint: dict[str, dict] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    by_category: dict[str, dict] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    by_reason: Counter = Counter()
    failures: list[dict] = []
    flaky: list[str] = []

    first_pass = 0
    final_pass = 0
    recovered = 0
    sut_defects = 0
    unknown = 0

    executed_ids = {r["case_id"] for r in records if r.get("case_id")}

    for record in records:
        case = cases_by_id.get(record.get("case_id", ""))
        endpoint = record.get("endpoint") or (case.endpoint if case else "")
        category = record.get("category") or (case.category if case else "")
        attempts = record.get("attempts") or []
        diagnosis = record.get("diagnosis") or {}
        first_ok = bool(attempts) and attempts[0].get("status") == "pass"
        final_ok = record.get("outcome") == "passed"

        by_endpoint[endpoint]["total"] += 1
        by_category[category]["total"] += 1
        by_endpoint[endpoint]["passed" if final_ok else "failed"] += 1
        by_category[category]["passed" if final_ok else "failed"] += 1
        first_pass += int(first_ok)
        final_pass += int(final_ok)
        if not first_ok and final_ok:
            recovered += 1
            if diagnosis.get("reason") == "env_flake":
                flaky.append(record.get("case_id", ""))
        if diagnosis.get("root_cause") == "code_defect":
            sut_defects += 1
        if diagnosis.get("reason") == "unknown" and not final_ok:
            unknown += 1
        if not final_ok:
            by_reason[diagnosis.get("reason", "unknown")] += 1
            failures.append(
                {
                    "case_id": record.get("case_id"),
                    "endpoint": endpoint,
                    "category": category,
                    "reason": diagnosis.get("reason"),
                    "root_cause": diagnosis.get("root_cause"),
                    "action_target": diagnosis.get("action_target"),
                    "detail": diagnosis.get("detail"),
                    "attempts": attempts,
                    "message": (record.get("message") or "")[:2000],
                }
            )

    unexecuted = [
        {"case_id": c.id, "endpoint": c.endpoint, "reason": "not_executed"}
        for c in cases
        if c.id not in executed_ids
    ]

    total = len(cases)
    executed = len(records)
    pass_rate = _rate(final_pass, executed)

    contract_incomplete = [
        p["endpoint"] for p in profiles if p.get("contract_incomplete")
    ]
    generated_endpoints = {c.endpoint for c in cases}
    changed_endpoints = {a["handler"] for a in impact.get("changed_apis", [])}
    endpoints_without_cases = sorted(changed_endpoints - generated_endpoints)

    blockers: list[str] = []
    warnings: list[str] = []
    if sut_defects:
        blockers.append(f"存在 {sut_defects} 个真实缺陷（code_defect）")
    if codegen.get("errors"):
        blockers.append(f"生成代码有 {len(codegen['errors'])} 个语法错误")
    has_unmapped = bool(
        unmapped.get("symbols")
        or unmapped.get("routes")
        or unmapped.get("profile_unmapped")
        or unmapped.get("unsupported_files")
        or contract_incomplete
        or endpoints_without_cases
    )
    if has_unmapped and not gate_config.get("allow_unmapped"):
        blockers.append("存在未映射范围（unmapped）：变更符号/路由/契约缺口未闭合")
    if executed == 0:
        warnings.append("未执行用例：未提供 base_url 或执行被跳过")
    if executed and pass_rate < float(gate_config.get("min_pass_rate", 0.95)):
        warnings.append(f"最终通过率 {pass_rate:.2%} 低于 {gate_config.get('min_pass_rate', 0.95):.0%}")
    if case_store:
        blockers.extend(case_store.get("gate_blockers") or [])
        warnings.extend(case_store.get("gate_warnings") or [])
        if case_store.get("degraded"):
            warnings.append(
                "本次运行使用已批准用例库的快照，但存在待审/漂移警告（degraded）"
            )
        if case_store.get("summary", {}).get("deprecated"):
            warnings.append(
                f"用例库存在 {case_store['summary']['deprecated']} 条 deprecated 用例，不参与执行"
            )

    route_health = (route_audit or {}).get("health") or {}
    if route_health.get("status") == "degraded":
        reasons = "，".join(route_health.get("reasons") or [])
        warnings.append(
            "路由清单可能不完整（route_inventory_degraded）"
            + (f"：{reasons}" if reasons else "")
            + "；受影响接口可能被少报"
        )

    if warnings:
        # 通过率不是门禁的唯一依据，但必须显式暴露
        pass

    if blockers:
        verdict = "fail"
    elif executed == 0:
        verdict = "not_run"
    else:
        verdict = "pass"

    report = {
        "run": run_meta,
        "summary": {
            "generated_cases": total,
            "executed_cases": executed,
            "first_pass": first_pass,
            "final_pass": final_pass,
            "first_pass_rate": _rate(first_pass, executed),
            "final_pass_rate": pass_rate,
            "sut_defects": sut_defects,
            "recovered_by_retry": recovered,
            "unknown_attribution": unknown,
            "flaky_candidates": flaky,
            "unexecuted_cases": len(unexecuted),
        },
        "gate": {
            "verdict": verdict,
            "blockers": blockers,
            "warnings": warnings,
            "config": gate_config,
        },
        "changes": changes,
        "impact": impact,
        "coverage": {
            "changed_endpoints": len(changed_endpoints),
            "profiled_endpoints": len(profiles),
            "generated_endpoints": len(generated_endpoints),
            "executed_endpoints": len({r.get("endpoint") for r in records if r.get("endpoint")}),
            "contract_incomplete": sorted(contract_incomplete),
            "changed_endpoints_without_cases": endpoints_without_cases,
        },
        "case_store": case_store,
        "routes": route_audit,
        "unmapped": {
            **unmapped,
            "contract_incomplete": sorted(contract_incomplete),
            "changed_endpoints_without_cases": endpoints_without_cases,
        },
        "by_endpoint": dict(by_endpoint),
        "by_category": dict(by_category),
        "by_reason": dict(by_reason),
        "failures": failures,
        "unexecuted": unexecuted,
        "codegen": codegen,
    }
    return report


def render_markdown(report: dict) -> str:
    run = report["run"]
    summary = report["summary"]
    gate = report["gate"]
    lines: list[str] = []
    lines.append(f"# AstSight 运行报告 · {run.get('run_id', '')}")
    lines.append("")
    lines.append(f"- 模式：`{run.get('mode')}`")
    lines.append(f"- 项目：`{run.get('project')}`")
    if run.get("base"):
        lines.append(f"- 范围：`{run.get('base')}...{run.get('target')}`（{run.get('comparison')}）")
    lines.append(f"- 门禁：**{gate['verdict'].upper()}**")
    if gate["blockers"]:
        for blocker in gate["blockers"]:
            lines.append(f"  - ⛔ {blocker}")
    for warning in gate["warnings"]:
        lines.append(f"  - ⚠️ {warning}")
    lines.append("")

    lines.append("## 结果概览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 生成用例 | {summary['generated_cases']} |")
    lines.append(f"| 执行用例 | {summary['executed_cases']} |")
    lines.append(f"| 首次通过 | {summary['first_pass']} ({summary['first_pass_rate']:.1%}) |")
    lines.append(f"| 最终通过 | {summary['final_pass']} ({summary['final_pass_rate']:.1%}) |")
    lines.append(f"| 真实缺陷 | {summary['sut_defects']} |")
    lines.append(f"| 重试恢复 | {summary['recovered_by_retry']} |")
    lines.append(f"| 未执行 | {summary['unexecuted_cases']} |")
    if report.get("case_store"):
        store = report["case_store"]
        coverage = store.get("requirement_coverage") or {}
        lines.append(
            f"| 需求覆盖 | {coverage.get('covered', 0)}/{coverage.get('total', 0)}"
            f" ({float(coverage.get('rate') or 0):.1%}) |"
        )
        lines.append(f"| 待审 proposal | {store.get('pending_proposals', 0)} |")
    lines.append("")

    impact = report["impact"]
    scope = impact.get("scope")
    if scope == "cases":
        lines.append("## 本次接口范围（已审核用例）")
    elif scope == "full":
        lines.append("## 本次接口范围（全量）")
    else:
        lines.append("## 本次变更影响的接口")
    lines.append("")
    changed = impact.get("changed_apis") or []
    if not changed:
        lines.append("_无（或未执行变更感知）_")
    else:
        lines.append("| 接口 | 置信度 | 证据 | 归因 | 变更符号 |")
        lines.append("|---|---|---|---|---|")
        for api in changed:
            lines.append(
                f"| `{api['handler']}` | {api['confidence']} | {api['evidence']} | "
                f"{api.get('attribution') or '-'} | "
                f"{', '.join(api.get('changed_symbols', [])[:4])} |"
            )
    lines.append("")

    if report.get("case_store"):
        store = report["case_store"]
        store_summary = store.get("summary") or {}
        coverage = store.get("requirement_coverage") or {}
        lines.append("## 用例审核与版本")
        lines.append("")
        lines.append(
            f"- 用例库：`{store.get('case_base', '')}`，store v{store.get('store_version', 0)}"
        )
        lines.append(
            f"- 已批准 {store_summary.get('approved', 0)} / 废弃 "
            f"{store_summary.get('deprecated', 0)} / 历史版本 "
            f"{store_summary.get('history_versions', 0)}"
        )
        lines.append(
            f"- 最近批准：{store.get('approved_at') or '-'} by "
            f"{store.get('approved_by') or '-'}"
        )
        pending = store.get("pending_proposals") or 0
        lines.append(
            f"- 待审 proposal：{pending}"
            + (
                "（" + ", ".join(store.get("pending_proposal_paths") or []) + "）"
                if pending else ""
            )
        )
        lines.append(
            f"- 需求覆盖：{coverage.get('covered', 0)}/{coverage.get('total', 0)}"
            f" ({float(coverage.get('rate') or 0):.1%})"
        )
        contract = store.get("contract_drift") or {}
        if contract.get("checked"):
            lines.append(
                f"- OpenAPI 漂移：{'有' if contract.get('changed') else '无'}"
            )
        else:
            lines.append("- OpenAPI 漂移：未校验")
        requirement_drift = store.get("requirement_drift") or {}
        if requirement_drift.get("checked"):
            changed_docs = requirement_drift.get("changed_docs") or []
            missing_docs = requirement_drift.get("missing_docs") or []
            lines.append(
                f"- 需求文档漂移：变更 {len(changed_docs)} / 缺失 {len(missing_docs)}"
            )
        uncovered = coverage.get("uncovered") or []
        if uncovered:
            lines.append("")
            lines.append("<details><summary>未覆盖需求</summary>")
            for item in uncovered[:50]:
                lines.append(
                    f"- `{item.get('doc')}#{item.get('section')}`"
                    f"：`{item.get('reason')}`"
                )
            lines.append("</details>")
        review_tail = store.get("review_log_tail") or []
        if review_tail:
            lines.append("")
            lines.append("<details><summary>最近审核记录</summary>")
            for item in review_tail[-10:]:
                lines.append(
                    f"- {item.get('at')} `{item.get('case_id') or '-'}` "
                    f"{item.get('action')} {item.get('decision')} "
                    f"by {item.get('reviewer')}"
                )
            lines.append("</details>")
        lines.append("")

    lines.append("## 覆盖边界（unmapped）")
    lines.append("")
    route_health = (report.get("routes") or {}).get("health") or {}
    if route_health:
        lines.append(
            f"- 路由清单健康度：{route_health.get('status')}"
            f"（可归因路由 {route_health.get('attributed_routes', 0)}"
            f" / 总路由 {route_health.get('total_routes', 0)}"
            f" / Controller 文件 {route_health.get('controller_files') or '-'}"
            f" / OpenAPI operation {route_health.get('openapi_operations') or '-'}）"
        )
        for reason in route_health.get("reasons") or []:
            lines.append(f"  - ⚠️ `{reason}`")
    unmapped = report["unmapped"]
    lines.append(f"- 未追溯符号：{len(unmapped.get('symbols', []))}")
    lines.append(f"- 路由冲突/不可判：{len(unmapped.get('routes', []))}")
    lines.append(f"- 画像未闭环：{len(unmapped.get('profile_unmapped', []))}")
    lines.append(f"- 语言不支持文件：{len(unmapped.get('unsupported_files', []))}")
    lines.append(f"- 契约缺失接口：{len(unmapped.get('contract_incomplete', []))}")
    lines.append(f"- 变更接口无用例：{len(unmapped.get('changed_endpoints_without_cases', []))}")
    if unmapped.get("symbols"):
        lines.append("")
        lines.append("<details><summary>未追溯符号明细</summary>")
        for item in unmapped["symbols"][:50]:
            lines.append(f"- `{item.get('symbol')}`（{item.get('reason')}）")
        lines.append("</details>")
    lines.append("")

    lines.append("## 失败明细")
    lines.append("")
    failures = report.get("failures") or []
    if not failures:
        lines.append("_无失败_")
    else:
        lines.append("| 用例 | 接口 | 类别 | 归因 | 归属 | 尝试 |")
        lines.append("|---|---|---|---|---|---|")
        for failure in failures:
            attempts = " → ".join(
                f"{a.get('n')}:{a.get('status')}" for a in failure.get("attempts", [])
            )
            lines.append(
                f"| {failure['case_id']} | `{failure['endpoint']}` | {failure['category']} | "
                f"{failure.get('reason')} | {failure.get('action_target') or '-'} | {attempts} |"
            )
    lines.append("")

    if report.get("unexecuted"):
        lines.append("## 未执行用例")
        lines.append("")
        for item in report["unexecuted"][:30]:
            lines.append(f"- {item['case_id']} `{item['endpoint']}`（{item['reason']}）")
        lines.append("")

    lines.append("## 产物")
    lines.append("")
    for name, path in (report.get("artifacts") or {}).items():
        lines.append(f"- `{name}`：`{path}`")
    lines.append("")
    return "\n".join(lines)
