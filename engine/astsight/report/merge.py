"""C7.4 报告合并。

门禁口径（对齐设计文档 C7.4）：
- **只认真实缺陷**（`code_defect`）与覆盖边界（`unmapped`），不认“重试后通过率”；
- 未执行/未生成的接口单列，不得计入通过率分母；
- 重试恢复单列 `recovered`，作为稳定性信号，不是质量结论。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from contracts import AssertStrength


def _contract_parts(diff: dict) -> str:
    parts: list[str] = []
    for key, label in (
        ("added", "新增"),
        ("removed", "删除"),
        ("required_added", "新增必填"),
        ("required_removed", "取消必填"),
        ("added_status_codes", "新增状态码"),
        ("removed_status_codes", "删除状态码"),
        ("success_added", "新增成功码"),
        ("success_removed", "删除成功码"),
    ):
        values = diff.get(key) or []
        if values:
            parts.append(f"{label} {', '.join(str(item) for item in values)}")
    changed = diff.get("type_changed") or []
    if changed:
        parts.append(
            "类型变化 "
            + ", ".join(
                f"{item['field']}: {item['before']} → {item['after']}"
                for item in changed
            )
        )
    fields = diff.get("fields") or {}
    for code, item in sorted(fields.items()):
        if item.get("added"):
            parts.append(f"{code} 字段新增 {', '.join(item['added'])}")
        if item.get("removed"):
            parts.append(f"{code} 字段删除 {', '.join(item['removed'])}")
    return "，".join(parts)


def _transport_contract_parts(item: dict) -> list[str]:
    parts = ["变化 " + ", ".join(item.get("change_kinds") or [])]
    for label in ("request", "response"):
        diff = item.get(label) or {}
        bits = []
        if diff.get("added"):
            bits.append(f"新增 {len(diff['added'])}")
        if diff.get("removed"):
            bits.append(f"删除 {len(diff['removed'])}")
        if diff.get("changed"):
            bits.append(f"变更 {len(diff['changed'])}")
        if bits:
            parts.append(f"{label}字段 " + "，".join(bits))
    if item.get("return_type"):
        diff = item["return_type"]
        parts.append(
            f"返回类型 {diff['before']} → {diff['after']}"
        )
    if item.get("parameter_types"):
        diff = item["parameter_types"]
        parts.append(
            "参数类型 "
            + f"{diff['before']} → {diff['after']}"
        )
    for label in ("parameter_schema", "result_schema"):
        diff = item.get(label) or {}
        bits = []
        if diff.get("added"):
            bits.append(f"新增 {len(diff['added'])}")
        if diff.get("removed"):
            bits.append(f"删除 {len(diff['removed'])}")
        if diff.get("changed"):
            bits.append(f"变更 {len(diff['changed'])}")
        if bits:
            parts.append(f"{label} " + "，".join(bits))
    return parts


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
    mock_evidence: dict | None = None,
    data_plan: dict | None = None,
    rules: dict | None = None,
) -> dict:
    gate_config = gate_config or {}
    cases_by_id = {c.id: c for c in cases}

    def _bucket() -> dict:
        return {"total": 0, "passed": 0, "failed": 0, "blocked": 0}

    by_endpoint: dict[str, dict] = defaultdict(_bucket)
    by_category: dict[str, dict] = defaultdict(_bucket)
    by_transport: dict[str, dict] = defaultdict(_bucket)
    by_reason: Counter = Counter()
    failures: list[dict] = []
    blocked_records: list[dict] = []
    flaky: list[str] = []

    first_pass = 0
    final_pass = 0
    recovered = 0
    sut_defects = 0
    unknown = 0
    blocked = 0

    executed_ids = {r["case_id"] for r in records if r.get("case_id")}

    for record in records:
        case = cases_by_id.get(record.get("case_id", ""))
        endpoint = record.get("endpoint") or (case.endpoint if case else "")
        category = record.get("category") or (case.category if case else "")
        attempts = record.get("attempts") or []
        diagnosis = record.get("diagnosis") or {}
        first_ok = bool(attempts) and attempts[0].get("status") == "pass"
        final_ok = record.get("outcome") == "passed"
        is_blocked = record.get("outcome") == "blocked"

        transport_value = getattr(case, "transport", None) if case is not None else None
        transport = (
            getattr(transport_value, "value", None)
            or (str(transport_value) if transport_value is not None else "unknown")
        )
        by_endpoint[endpoint]["total"] += 1
        by_category[category]["total"] += 1
        by_transport[transport]["total"] += 1
        bucket = "passed" if final_ok else ("blocked" if is_blocked else "failed")
        by_endpoint[endpoint][bucket] += 1
        by_category[category][bucket] += 1
        by_transport[transport][bucket] += 1
        first_pass += int(first_ok)
        final_pass += int(final_ok)
        if is_blocked:
            # 造数/DB 未就绪是用例未真正执行，不计入通过率分母，也不伪装为失败。
            blocked += 1
            blocked_records.append(
                {
                    "case_id": record.get("case_id"),
                    "endpoint": endpoint,
                    "category": category,
                    "reason": diagnosis.get("reason") or "data_unavailable",
                    "message": (record.get("message") or "")[:2000],
                }
            )
            continue
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
    runnable = executed - blocked
    pass_rate = _rate(final_pass, runnable)

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
    if blocked:
        blockers.append(
            f"存在 {blocked} 条用例因造数前置未就绪被阻断（blocked/data_unavailable）"
        )
    if runnable and pass_rate < float(gate_config.get("min_pass_rate", 0.95)):
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

    mock = mock_evidence or {
        "checked": False,
        "reason": "mock_evidence_not_configured",
    }
    executed_case_ids = {
        str(record.get("case_id") or "")
        for record in records
        if record.get("case_id")
    }
    weak_oracle: list[str] = []
    if mock.get("checked"):
        mock_by_case = mock.get("by_case") or {}
        for case in cases:
            if case.id not in executed_case_ids:
                continue
            if not getattr(case, "mock_bindings", None):
                continue
            if getattr(case, "assert_strength", None) != AssertStrength.L1:
                continue
            calls = int((mock_by_case.get(case.id) or {}).get("calls") or 0)
            if calls == 0:
                weak_oracle.append(case.id)
        if weak_oracle:
            warnings.append(
                f"{len(weak_oracle)} 条用例只有状态码断言且 Mock 未命中"
                "（weak_oracle）"
            )
    if mock.get("checked"):
        blocking = int(mock.get("blocking_errors") or 0)
        if blocking:
            blockers.append(
                f"Mock 调用存在 {blocking} 个阻断类错误"
                "（unknown/ambiguous/unbound/branch_missing）"
            )
        by_result = mock.get("by_result") or {}
        late = int(by_result.get("late") or 0)
        if late:
            warnings.append(
                f"Mock 晚到/过期调用 {late} 次：drain 窗口不足或 SUT 存在异步调用（degraded）"
            )
        if mock.get("invalid_lines"):
            warnings.append(
                f"mock_calls.jsonl 有 {mock['invalid_lines']} 行无法解析（degraded）"
            )
        if mock.get("unattributed_calls"):
            warnings.append(
                f"Mock 有 {mock['unattributed_calls']} 次调用无法归因到用例"
            )
        unused = int(mock.get("unused_binding_count") or 0)
        if unused:
            blockers.append(
                f"Mock 有 {unused} 个会话绑定的下游依赖零调用（mock_not_used）"
            )
        if mock.get("sessions_reason"):
            warnings.append(
                f"Mock 会话绑定未校验：{mock['sessions_reason']}（degraded）"
            )
        elif not mock.get("sessions_checked"):
            warnings.append(
                "Mock 会话绑定未校验：未提供会话历史（degraded）"
            )
        if mock.get("sessions_missing"):
            warnings.append(
                f"Mock 证据中有 {mock['sessions_missing']} 个会话不在会话历史中，"
                "mock_not_used 校验不完整（degraded）"
            )
    elif mock.get("reason") not in (None, "mock_evidence_not_configured"):
        warnings.append(f"Mock 证据未读取：{mock.get('reason')}")

    plan = mock.get("plan") or {}
    if plan:
        for item in plan.get("warnings") or []:
            warnings.append(f"Mock 计划告警：{item}")
        unsupported = int(plan.get("unsupported_count") or 0)
        if unsupported:
            warnings.append(
                f"Mock 计划有 {unsupported} 个下游无法注入（unsupported），"
                "相关用例可能未真正隔离"
            )

    dubbo_routes = int(plan.get("dubbo_route_count") or 0)
    if dubbo_routes:
        agent_state = mock.get("agent") or {}
        heartbeat = agent_state.get("heartbeat") or {}
        dubbo_agent = heartbeat.get("dubbo") or {}
        if not heartbeat:
            blockers.append(
                "计划内 Dubbo 下游未确认 Filter 在消费端链上"
                f"（filter_not_in_chain）：{agent_state.get('reason') or 'agent_heartbeat_missing'}"
            )
        elif not dubbo_agent.get("installed"):
            blockers.append(
                "计划内 Dubbo 下游已登记，但 agent 心跳未上报 Dubbo Filter"
                "（filter_not_in_chain：dubbo.installed=false）"
            )
        elif int(dubbo_agent.get("filter_instantiated") or 0) <= 0:
            blockers.append(
                "计划内 Dubbo 下游已登记，但消费端 Filter 从未实例化"
                "（filter_not_in_chain：dubbo.filter_instantiated=0）"
            )
        if heartbeat and not dubbo_agent.get("serialization_verified", False):
            warnings.append(
                "Dubbo 桩响应 serialization_verified=false："
                "跳过 Hessian2 链路，序列化保真未验证（degraded）"
            )

    mq_routes = int(plan.get("mq_route_count") or 0)
    if mq_routes:
        agent_state = mock.get("agent") or {}
        heartbeat = agent_state.get("heartbeat") or {}
        mq_agent = heartbeat.get("mq") or {}
        if not heartbeat:
            blockers.append(
                "计划内 MQ 下游未确认生产者拦截已安装"
                f"（mq_producer_not_installed）："
                f"{agent_state.get('reason') or 'agent_heartbeat_missing'}"
            )
        elif not mq_agent.get("installed"):
            blockers.append(
                "计划内 MQ 下游已登记，但 agent 心跳未上报 MQ 生产者拦截"
                "（mq_producer_not_installed：mq.installed=false）"
            )
        else:
            required_clients = {
                str(item) for item in plan.get("mq_clients") or [] if item
            }
            installed_clients = {
                str(item) for item in mq_agent.get("clients") or [] if item
            }
            missing = sorted(required_clients - installed_clients)
            if missing:
                blockers.append(
                    "计划内 MQ 客户端缺少生产者拦截模块："
                    f"{', '.join(missing)}（mq_producer_not_installed）"
                )

    contract_state = impact.get("contract_changes") or {}
    contract_count = (
        len(contract_state.get("changes") or [])
        + len((contract_state.get("grpc") or {}).get("changes") or [])
        + len((contract_state.get("dubbo") or {}).get("changes") or [])
    )
    if contract_count:
        warnings.append(
            f"本次变更涉及 {contract_count} 个接口的契约字段变化"
            "（contract_change）"
        )
    for item in contract_state.get("warnings") or []:
        warnings.append(f"契约 diff 未完成：{item}")

    deleted_state = impact.get("deleted_apis") or {}
    if deleted_state.get("deleted"):
        cleanup = deleted_state.get("case_cleanup") or {}
        deprecated = cleanup.get("deprecated") or []
        if deprecated:
            warnings.append(
                f"本次变更删除了 {len(deleted_state['deleted'])} 个接口，"
                f"已自动逻辑删除 {len(deprecated)} 条旧用例（deleted_api）"
            )
        else:
            warnings.append(
                f"本次变更删除了 {len(deleted_state['deleted'])} 个接口，"
                "旧用例可能已失效（deleted_api）"
            )
    for item in deleted_state.get("warnings") or []:
        warnings.append(f"删除接口扫描未完成：{item}")

    route_health = (route_audit or {}).get("health") or {}
    if route_health.get("status") == "degraded":
        reasons = "，".join(route_health.get("reasons") or [])
        warnings.append(
            "路由清单可能不完整（route_inventory_degraded）"
            + (f"：{reasons}" if reasons else "")
            + "；受影响接口可能被少报"
        )

    if rules:
        blockers.extend(rules.get("gate_blockers") or [])
        warnings.extend(rules.get("gate_warnings") or [])

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
            "first_pass_rate": _rate(first_pass, runnable),
            "final_pass_rate": pass_rate,
            "sut_defects": sut_defects,
            "recovered_by_retry": recovered,
            "unknown_attribution": unknown,
            "flaky_candidates": flaky,
            "weak_oracle": weak_oracle,
            "unexecuted_cases": len(unexecuted),
            "blocked_cases": blocked,
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
        "mock": mock,
        "data_plan": data_plan,
        "rules": rules,
        "blocked": blocked_records,
        "routes": route_audit,
        "unmapped": {
            **unmapped,
            "contract_incomplete": sorted(contract_incomplete),
            "changed_endpoints_without_cases": endpoints_without_cases,
        },
        "by_endpoint": dict(by_endpoint),
        "by_category": dict(by_category),
        "by_transport": dict(by_transport),
        "by_reason": dict(by_reason),
        "failures": failures,
        "unexecuted": unexecuted,
        "codegen": codegen,
        "audit": {
            "egress_guard": {"status": "not_implemented"},
            "weak_oracle": {
                "checked": bool(mock.get("checked")),
                "count": len(weak_oracle),
            },
            "mock_sessions": {
                "checked": bool(mock.get("sessions_checked")),
                "evidence": len(mock.get("session_evidence") or []),
                "truncated": bool(mock.get("session_evidence_truncated")),
                "binding_fields": "dep_id/branch/calls",
            },
            "transport_coverage": {"status": "not_implemented"},
            "transport_fallback": {"status": "not_applicable"},
            "instance": {"status": "not_applicable"},
            "token_usage": {"status": "not_available"},
        },
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
    lines.append(f"| 数据阻断（blocked） | {summary.get('blocked_cases', 0)} |")
    weak_oracle = summary.get("weak_oracle") or []
    if weak_oracle:
        lines.append(f"| 弱断言（weak_oracle） | {len(weak_oracle)} |")
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

    contract_state = impact.get("contract_changes") or {}
    if contract_state.get("checked"):
        changes = contract_state.get("changes") or []
        lines.append("## 契约字段变化")
        lines.append("")
        if not changes:
            lines.append("_无_")
        else:
            for item in changes:
                parts = []
                params = item.get("params") or {}
                body = item.get("request_body") or {}
                responses = item.get("responses") or {}
                if params:
                    parts.append("参数 " + _contract_parts(params))
                if body:
                    parts.append("请求体 " + _contract_parts(body))
                if responses:
                    parts.append("响应 " + _contract_parts(responses))
                lines.append(
                    f"- `{item.get('endpoint')}`："
                    + "；".join(part for part in parts if part)
                )
        for transport, label in (("grpc", "gRPC"), ("dubbo", "Dubbo")):
            section = contract_state.get(transport) or {}
            for item in section.get("changes") or []:
                parts = _transport_contract_parts(item)
                lines.append(
                    f"- `{label}` `{item.get('endpoint')}`："
                    + "；".join(parts)
                )
            if section.get("added_methods"):
                lines.append(
                    f"- `{label}` 新增：{', '.join(section['added_methods'])}"
                )
            if section.get("removed_methods"):
                lines.append(
                    f"- `{label}` 删除：{', '.join(section['removed_methods'])}"
                )
        lines.append("")

    deleted_state = impact.get("deleted_apis") or {}
    if deleted_state.get("checked"):
        deleted = deleted_state.get("deleted") or []
        lines.append("## 被删除的接口")
        lines.append("")
        if not deleted:
            lines.append("_无_")
        else:
            lines.append("| 接口 | handler | 文件 |")
            lines.append("|---|---|---|")
            for item in deleted:
                lines.append(
                    f"| `{item['endpoint']}` | {item.get('handler') or '-'} | "
                    f"{item.get('file') or '-'}:{item.get('line') or 0} |"
                )
        deprecated = (
            (deleted_state.get("case_cleanup") or {}).get("deprecated") or []
        )
        if deprecated:
            lines.append("")
            lines.append(
                f"已自动逻辑删除 {len(deprecated)} 条指向已删除接口的用例"
                "（deprecated，不进入运行）："
            )
            lines.append("")
            lines.append("| 用例 | 端点 | case_key |")
            lines.append("|---|---|---|")
            for item in deprecated:
                lines.append(
                    f"| `{item['id']}` | `{item['endpoint']}` | "
                    f"{item['case_key']} |"
                )
        lines.append("")
    elif deleted_state.get("warnings"):
        lines.append("## 被删除的接口")
        lines.append("")
        lines.append("_扫描未完成_：" + "；".join(deleted_state["warnings"][:5]))
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

    rules = report.get("rules") or {}
    if rules:
        lines.append("## 规则与覆盖")
        lines.append("")
        if not rules.get("checked"):
            lines.append(f"- 规则库未校验：`{rules.get('reason')}`")
            if rules.get("error"):
                lines.append(f"- 错误：`{str(rules['error'])[:500]}`")
            lines.append("")
        else:
            counts = rules.get("counts") or {}
            business = (rules.get("coverage") or {}).get("business") or {}
            contract = (rules.get("coverage") or {}).get("contract") or {}
            br_stats = business.get("stats") or {}
            cr_stats = contract.get("stats") or {}
            lines.append(
                f"- 规则库：`{rules.get('ruleset_id') or '-'}` v{rules.get('version')} "
                f"hash `{str(rules.get('hash') or '')[:12]}`"
            )
            if rules.get("path"):
                lines.append(f"- 规则文件：`{rules.get('path')}`")
            lines.append(
                f"- BR：{counts.get('business_rules', 0)} 条，"
                f"覆盖 {br_stats.get('covered', 0)}/{br_stats.get('active', 0)}"
                f" ({float(br_stats.get('ratio') or 0):.1%})"
            )
            lines.append(
                f"- CR：{counts.get('contract_rules', 0)} 条，"
                f"覆盖 {cr_stats.get('covered', 0)}/{cr_stats.get('active', 0)}"
                f" ({float(cr_stats.get('ratio') or 0):.1%})"
            )
            lines.append(
                f"- 待确认：问题 {len(rules.get('questions') or [])} / "
                f"缺口 {len(rules.get('gaps') or [])} / "
                f"冲突 {len(rules.get('conflicts') or [])}（用例审核时确认）"
            )
            drift = rules.get("rule_drift") or {}
            lines.append(
                f"- 规则漂移：{'有' if drift.get('changed') else '无'}"
                f"（store `{str(drift.get('store_hash') or '')[:12] or '-'}` → "
                f"current `{str(drift.get('current_hash') or '')[:12]}`）"
            )
            orphaned = rules.get("orphaned_rule_keys") or []
            if orphaned:
                lines.append(f"- 孤立规则引用：{len(orphaned)}（用例引用的规则已不存在）")
            stale_cases = rules.get("stale_rule_cases") or []
            if stale_cases:
                lines.append(f"- 引用非 active 规则的用例：{len(stale_cases)}")
            uncovered = [
                item
                for item in (business.get("rules") or [])
                if not item.get("covered") and item.get("status") == "active"
            ]
            if uncovered:
                lines.append("")
                lines.append("<details><summary>未覆盖的 BR 规则</summary>")
                for item in uncovered[:50]:
                    lines.append(
                        f"- `{item.get('rule_id')}` {item.get('rule_key')} "
                        f"({item.get('authority')})"
                    )
                lines.append("</details>")
            lines.append("")

    mock = report.get("mock") or {}
    if mock.get("checked"):
        lines.append("## 下游 Mock")
        lines.append("")
        lines.append(
            f"- 调用 {mock.get('calls', 0)} 次 / 会话 {mock.get('sessions', 0)} 个 / "
            f"无法归因 {mock.get('unattributed_calls', 0)} 次"
        )
        by_result = mock.get("by_result") or {}
        lines.append(
            "- 结果：" + "，".join(f"{key}={value}" for key, value in by_result.items())
        )
        if mock.get("sessions_checked"):
            lines.append(
                f"- 会话绑定：{mock.get('session_records', 0)} 个会话 / "
                f"未命中 `dep_id` {mock.get('unused_binding_count', 0)} 个"
            )
        if mock.get("sessions_missing"):
            lines.append(
                f"- 会话历史缺失：{mock['sessions_missing']} 个证据会话未出现在历史中"
            )
        if mock.get("degraded"):
            lines.append("- 状态：**degraded**（存在晚到、阻断类错误或损坏证据）")
        unused = mock.get("unused_bindings") or []
        if unused:
            lines.append("")
            lines.append(
                f"<details><summary>绑定但零调用（{mock.get('unused_binding_count', 0)}）</summary>"
            )
            for item in unused:
                lines.append(
                    f"- `{item.get('case_id') or '-'}` / `{item.get('dep_id') or '-'}` "
                    f"→ `{item.get('branch') or '-'}`（session `{item.get('session_id')}`）"
                )
            lines.append("</details>")
        late_calls = mock.get("late_calls") or []
        if late_calls:
            lines.append("")
            lines.append(f"<details><summary>晚到调用（{len(late_calls)}）</summary>")
            for call in late_calls:
                lines.append(
                    f"- `{call.get('case_id') or '-'}` / `{call.get('dep_id') or '-'}` "
                    f"`{call.get('path')}`：{call.get('error') or '-'} at {call.get('at')}"
                )
            lines.append("</details>")
        errors = mock.get("errors") or []
        if errors:
            lines.append("")
            lines.append(f"<details><summary>阻断类调用（{len(errors)}）</summary>")
            for call in errors:
                lines.append(
                    f"- `{call.get('case_id') or '-'}` / `{call.get('dep_id') or '-'}` "
                    f"`{call.get('path')}`：`{call.get('result')}` "
                    f"{call.get('error') or '-'}"
                )
            lines.append("</details>")
        evidence = mock.get("session_evidence") or []
        if evidence:
            lines.append("")
            lines.append(
                f"<details><summary>逐用例会话证据（{len(evidence)}）</summary>"
            )
            for item in evidence:
                calls = sum(
                    int(binding.get("calls") or 0)
                    for binding in item.get("bindings") or []
                )
                lines.append(
                    f"- `{item.get('case_id') or '-'}` / session "
                    f"`{item.get('session_id') or '-'}`："
                    f"状态 `{item.get('status') or '-'}`，调用 {calls} 次"
                )
            lines.append("</details>")
        lines.append("")
    agent_state = mock.get("agent") or {}
    if int((mock.get("plan") or {}).get("dubbo_route_count") or 0):
        heartbeat = agent_state.get("heartbeat") or {}
        dubbo_agent = heartbeat.get("dubbo") or {}
        lines.append("## Dubbo Filter")
        lines.append("")
        if not heartbeat:
            lines.append(
                f"- Filter 未确认在链上：`{agent_state.get('reason') or 'agent_heartbeat_missing'}`"
            )
        else:
            lines.append(
                f"- 版本：`{dubbo_agent.get('version') or '-'}` / "
                f"实例化 {dubbo_agent.get('filter_instantiated', 0)} 次 / "
                f"命中 {dubbo_agent.get('filter_hits', 0)} 次 / "
                f"放行 {dubbo_agent.get('passthrough', 0)} 次 / "
                f"错误 {dubbo_agent.get('errors', 0)} 次"
            )
            lines.append(
                "- 序列化保真："
                + ("已验证" if dubbo_agent.get("serialization_verified") else "未验证（serialization_verified=false）")
            )
        lines.append("")
    elif mock.get("reason") not in (None, "mock_evidence_not_configured"):
        lines.append("## 下游 Mock")
        lines.append("")
        lines.append(f"- 证据未读取：`{mock.get('reason')}`")
        lines.append("")

    if int((mock.get("plan") or {}).get("mq_route_count") or 0):
        heartbeat = agent_state.get("heartbeat") or {}
        mq_agent = heartbeat.get("mq") or {}
        lines.append("## MQ 生产者拦截")
        lines.append("")
        if not heartbeat:
            lines.append(
                f"- 生产者拦截未确认：`{agent_state.get('reason') or 'agent_heartbeat_missing'}`"
            )
        else:
            clients = ", ".join(mq_agent.get("clients") or []) or "-"
            lines.append(
                f"- 客户端：{clients} / 已上报 {mq_agent.get('published', 0)} 次 / "
                f"拦截 {mq_agent.get('suppressed', 0)} 次 / "
                f"错误 {mq_agent.get('errors', 0)} 次"
            )
            lines.append("- 消息未真正发送到 broker；只验证 SUT 准备发出的内容")
        lines.append("")

    plan = mock.get("plan") or {}
    if plan:
        lines.append("## 下游 Mock 计划")
        lines.append("")
        lines.append(
            f"- 模式：`{plan.get('mode')}` / SUT `{plan.get('sut_id')}` "
            f"/ namespace `{plan.get('namespace')}`"
        )
        if plan.get("plan"):
            lines.append(f"- 计划文件：`{plan.get('plan')}`")
        if plan.get("endpoint"):
            lines.append(f"- 内嵌地址：`{plan.get('endpoint')}`（不含 token）")
        lines.append(
            f"- 路由 {plan.get('route_count', 0)} 个 / 注入 {plan.get('injection_count', 0)} 个"
            f" / unsupported {plan.get('unsupported_count', 0)} 个"
        )
        if plan.get("warnings"):
            lines.append("")
            lines.append("<details><summary>计划告警</summary>")
            for item in plan["warnings"]:
                lines.append(f"- `{item}`")
            lines.append("</details>")
        lines.append("")

    data_plan = report.get("data_plan") or {}
    if data_plan:
        lines.append("## 造数计划")
        lines.append("")
        lines.append(f"- 运行域：`{data_plan.get('run_scope') or '-'}`")
        lines.append(
            f"- 计划数：{data_plan.get('plans', 0)} / "
            f"数据库：{'已配置' if data_plan.get('database_url_configured') else '未配置'}"
        )
        if data_plan.get("path"):
            lines.append(f"- 计划文件：`{data_plan.get('path')}`")
        missing = data_plan.get("missing_plans") or []
        if missing:
            lines.append(
                f"- 缺少造数计划的前置用例：{len(missing)}（"
                + ", ".join(str(item) for item in missing[:20]) + "）"
            )
        lines.append("")

    blocked = report.get("blocked") or []
    if blocked:
        lines.append("## 数据阻断（blocked）")
        lines.append("")
        for item in blocked[:50]:
            lines.append(
                f"- {item.get('case_id') or '-'} `{item.get('endpoint')}`："
                f"`{item.get('reason')}` {item.get('message') or ''}"
            )
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

    audit = report.get("audit") or {}
    if audit:
        lines.append("## 审计完整性")
        lines.append("")
        labels = {
            "not_implemented": "未实现",
            "not_applicable": "不适用",
            "not_available": "无数据",
        }
        for name, item in audit.items():
            status = item.get("status")
            if status in labels:
                lines.append(f"- `{name}`：{labels[status]}")
            elif item.get("checked") is False:
                lines.append(f"- `{name}`：未检查")
            elif item.get("checked") is not None:
                detail = "已检查"
                if name == "weak_oracle":
                    detail += f"，{item.get('count', 0)} 条弱断言"
                if name == "mock_sessions":
                    detail += f"，{item.get('evidence', 0)} 条会话证据"
                lines.append(f"- `{name}`：{detail}")
        lines.append("")

    usage = report.get("token_usage") or {}
    if usage.get("calls"):
        lines.append("## Token 用量")
        lines.append("")
        lines.append(
            f"- 调用 {usage.get('calls', 0)} 次 / 输入 "
            f"{usage.get('total_input_tokens', 0)} / 输出 "
            f"{usage.get('total_output_tokens', 0)}"
        )
        if usage.get("by_node_status") == "not_available":
            lines.append("- 按节点统计不可用：当前 LLMUsage 未记录节点标签")
        lines.append("")

    lines.append("## 产物")
    lines.append("")
    for name, path in (report.get("artifacts") or {}).items():
        lines.append(f"- `{name}`：`{path}`")
    lines.append("")
    return "\n".join(lines)
