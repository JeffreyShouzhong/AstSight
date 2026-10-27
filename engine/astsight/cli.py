"""AstSight 命令行入口。

    python -m astsight cases prepare ...
    python -m astsight cases import ...
    python -m astsight cases approve ...
    python -m astsight run --mode cases ...

退出码对齐报告门禁：0=pass，1=fail，2=not_run（未执行/无可执行范围）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .cases.context import load_context, prepare_context
from .cases.draft import load_draft
from .cases.proposal import (
    ProposalError,
    approve_proposal,
    build_proposal,
    classify,
    list_pending_proposals,
)
from .cases.store import (
    case_summary,
    empty_case_set,
    load_case_set,
    requirement_coverage,
    resolve_case_base,
)
from .cases.validate import validate_draft
from .pipeline import RunConfig, run

_EXIT = {"pass": 0, "fail": 1, "not_run": 2}


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _write_import_validation(project: Path, validation) -> Path:
    target = (
        project
        / ".astsight"
        / "cases"
        / "imports"
        / _now_id()
        / "validation.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(validation.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return target


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astsight",
        description="AstSight：需求→审核用例→版本化→pytest 的 API 自动化测试框架",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="执行 full/delta/cases 运行")
    run_parser.add_argument("--project", required=True, help="被测项目路径")
    run_parser.add_argument("--mode", choices=("delta", "full", "cases"), default="delta")
    run_parser.add_argument("--base", default="main", help="delta 基线（分支/commit）")
    run_parser.add_argument("--target", default="HEAD", help="delta 目标")
    run_parser.add_argument("--base-url", default=None, help="被测服务地址；不提供则只生成不执行")
    run_parser.add_argument("--reset-url", default=None)
    run_parser.add_argument("--openapi", default=None, help="OpenAPI 文件或 URL；缺省自动发现")
    run_parser.add_argument("--out", default=".astsight/runs", help="产物根目录（相对 project）")
    run_parser.add_argument("--depth", type=int, default=5)
    run_parser.add_argument("--max-cases-per-endpoint", type=int, default=8)
    run_parser.add_argument("--skip-execute", action="store_true")
    run_parser.add_argument("--allow-unmapped", action="store_true")
    run_parser.add_argument("--retries", type=int, default=1)
    run_parser.add_argument("--no-collect-check", action="store_true")
    run_parser.add_argument("--codegraph", choices=("auto", "on", "off"), default="auto")
    run_parser.add_argument("--codegraph-module-root", default=None)
    run_parser.add_argument("--full-index", action="store_true")
    run_parser.add_argument("--timeout", type=float, default=600.0)
    run_parser.add_argument("--min-pass-rate", type=float, default=0.95)
    run_parser.add_argument(
        "--case-base", default=None,
        help="项目级用例库路径（相对 project；默认 caseset.json）",
    )
    run_parser.add_argument(
        "--fail-on-pending-proposals", action="store_true",
        help="存在待审 proposal 时作为门禁 blocker（默认只 warning + degraded）",
    )
    run_parser.add_argument(
        "--min-requirement-coverage", type=float, default=None,
        help="需求覆盖率门禁；默认不设阈值",
    )
    run_parser.add_argument(
        "--allow-stale-cases", action="store_true",
        help="approved case endpoint 在 OpenAPI 中缺失时降级为 warning",
    )
    run_parser.add_argument("--json", action="store_true", help="把报告摘要打印为 JSON")

    cases_parser = sub.add_parser("cases", help="需求→用例上下文、导入、审核与用例库")
    cases_sub = cases_parser.add_subparsers(dest="cases_command", required=True)

    prepare = cases_sub.add_parser("prepare", help="生成 context/prompt/schema 上下文包")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--requirements", action="append", required=True, metavar="PATH")
    prepare.add_argument("--openapi", default=None)
    prepare.add_argument("--base-url", default=None)
    prepare.add_argument("--out", default=".astsight/casegen", help="相对 project")
    prepare.add_argument("--json", action="store_true")

    imp = cases_sub.add_parser("import", help="校验草案并生成待审 proposal")
    imp.add_argument("--project", required=True)
    imp.add_argument("--context", required=True, help="context.json 或其目录")
    imp.add_argument("--draft", required=True)
    imp.add_argument("--case-base", default=None)
    imp.add_argument("--dry-run", action="store_true", help="只校验/diff，不写 proposal")
    imp.add_argument("--json", action="store_true")

    approve = cases_sub.add_parser("approve", help="按审核文件把批准项写入 caseset.json")
    approve.add_argument("--project", required=True)
    approve.add_argument("--proposal", required=True, help="proposal 目录或 proposal.json")
    approve.add_argument("--case-base", default=None)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--json", action="store_true")

    status = cases_sub.add_parser("status", help="查看用例库/待审/需求覆盖")
    status.add_argument("--project", required=True)
    status.add_argument("--case-base", default=None)
    status.add_argument("--json", action="store_true")
    return parser


def _utf8_stdio() -> None:
    """Windows 控制台默认 GBK，报告里的中文/符号会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _run_command(args) -> int:
    retries = max(0, min(3, args.retries))
    config = RunConfig(
        project=Path(args.project).resolve().as_posix(),
        mode=args.mode,
        base=args.base,
        target=args.target,
        base_url=args.base_url,
        reset_url=args.reset_url,
        openapi=args.openapi,
        out=args.out,
        depth=args.depth,
        max_cases_per_endpoint=args.max_cases_per_endpoint,
        skip_execute=args.skip_execute,
        allow_unmapped=args.allow_unmapped,
        retries=retries,
        collect_only=not args.no_collect_check,
        codegraph=args.codegraph,
        codegraph_module_root=args.codegraph_module_root,
        full_index=True if args.full_index else None,
        timeout=args.timeout,
        min_pass_rate=args.min_pass_rate,
        case_base=args.case_base,
        fail_on_pending_proposals=args.fail_on_pending_proposals,
        min_requirement_coverage=args.min_requirement_coverage,
        allow_stale_cases=args.allow_stale_cases,
    )
    try:
        report = run(config)
    except Exception as exc:
        print(f"AstSight 运行中止: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_summary(report), ensure_ascii=False, indent=2))
    else:
        _print_summary(report)
    verdict = report.get("gate", {}).get("verdict", "fail")
    return _EXIT.get(verdict, 1)


def _cases_prepare(args) -> int:
    project = Path(args.project).resolve()
    try:
        prepared = prepare_context(
            project,
            list(args.requirements),
            openapi=args.openapi,
            base_url=args.base_url,
            out=args.out,
        )
    except Exception as exc:
        print(f"cases prepare 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = {
        "context_id": prepared.context_id,
        "context_dir": str(prepared.context_dir),
        "context": str(prepared.context_path),
        "prompt": str(prepared.prompt_path),
        "schema": str(prepared.schema_path),
        "endpoints": len(prepared.context.get("profiles") or []),
        "documents": len(prepared.context.get("documents") or []),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cases_import(args) -> int:
    project = Path(args.project).resolve()
    try:
        context = load_context(args.context)
        draft = load_draft(args.draft)
        validation = validate_draft(draft, context, project=project)
    except Exception as exc:
        print(f"cases import 读取失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if validation.has_errors:
        validation_path = _write_import_validation(project, validation)
        print(
            json.dumps(
                {
                    "ok": False,
                    "validation": str(validation_path),
                    "errors": [item.model_dump() for item in validation.errors],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    store_path = resolve_case_base(project, args.case_base)
    store = load_case_set(store_path) or empty_case_set(project, context=context)
    if args.dry_run:
        if not draft.cases and (store.cases or store.history):
            print("草案为空但用例库非空，拒绝导入以防误清空", file=sys.stderr)
            return 1
        diff = classify(validation, store, scope_docs=validation.scope_docs)
        payload = validation.as_dict()
        payload.update(
            {
                "dry_run": True,
                "counts": diff.counts(),
                "case_base": str(store_path),
                "items": [
                    {
                        "proposal_key": item.proposal_key,
                        "action": item.action,
                        "endpoint": item.endpoint,
                        "case_key": item.case_key,
                        "store_case_id": item.store_case_id,
                        "current_version": item.current_version,
                        "proposed_version": item.proposed_version,
                    }
                    for item in diff.actionable
                ],
            }
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    try:
        result = build_proposal(
            project=project,
            draft_path=args.draft,
            draft=draft,
            context=context,
            validation=validation,
            store=store,
            store_path=store_path,
        )
    except Exception as exc:
        print(f"cases import 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = {
        "ok": True,
        "actionable": result.has_actionable,
        "proposal": str(result.path) if result.path else None,
        "reused": result.reused,
        "counts": result.diff.counts(),
        "case_base": str(store_path),
    }
    if not result.has_actionable:
        payload["message"] = "无变更：所有用例 unchanged/kept_out_of_scope"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cases_approve(args) -> int:
    project = Path(args.project).resolve()
    try:
        store_path = resolve_case_base(project, args.case_base)
        approval = approve_proposal(
            proposal_path=args.proposal,
            project=project,
            case_base=store_path,
            reviewer=args.reviewer,
        )
    except ProposalError as exc:
        print(f"cases approve 拒绝: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"cases approve 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(approval, ensure_ascii=False, indent=2))
    return 0


def _cases_status(args) -> int:
    project = Path(args.project).resolve()
    store_path = resolve_case_base(project, args.case_base)
    try:
        store = load_case_set(store_path)
    except Exception as exc:
        print(f"cases status 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    pending = list_pending_proposals(project)
    coverage = (
        requirement_coverage(store)
        if store is not None
        else {"total": 0, "covered": 0, "rate": 0.0, "uncovered": []}
    )
    payload = {
        "case_base": str(store_path),
        "exists": store is not None,
        "store_version": store.store_version if store is not None else 0,
        "approved_at": store.approved_at if store is not None else None,
        "approved_by": store.approved_by if store is not None else None,
        "summary": (
            case_summary(store)
            if store is not None
            else {"approved": 0, "deprecated": 0, "history_versions": 0}
        ),
        "pending_proposals": pending,
        "requirement_coverage": coverage,
        "deprecated_cases": (
            [
                {
                    "case_id": entry.id,
                    "case_key": entry.case_key,
                    "endpoint": entry.endpoint,
                    "version": entry.version,
                }
                for entry in store.cases
                if entry.status.value == "deprecated"
            ]
            if store is not None else []
        ),
        "review_log_tail": (
            [event.model_dump(mode="json") for event in store.review_log[-10:]]
            if store is not None else []
        ),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"case store: {payload['case_base']} (v{payload['store_version']})")
        print(
            f"  approved {payload['summary'].get('approved', 0)} / "
            f"deprecated {payload['summary'].get('deprecated', 0)} / "
            f"history {payload['summary'].get('history_versions', 0)}"
        )
        print(
            f"  需求覆盖: {coverage.get('covered', 0)}/{coverage.get('total', 0)} "
            f"({float(coverage.get('rate') or 0):.1%})"
        )
        print(f"  待审 proposal: {len(pending)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    if args.command == "cases":
        if args.cases_command == "prepare":
            return _cases_prepare(args)
        if args.cases_command == "import":
            return _cases_import(args)
        if args.cases_command == "approve":
            return _cases_approve(args)
        if args.cases_command == "status":
            return _cases_status(args)
    parser.print_help()
    return 2


def _summary(report: dict) -> dict:
    return {
        "run_id": report.get("run", {}).get("run_id"),
        "verdict": report.get("gate", {}).get("verdict"),
        "summary": report.get("summary"),
        "gate": report.get("gate"),
        "coverage": report.get("coverage"),
        "case_store": report.get("case_store"),
        "report_md": (report.get("artifacts") or {}).get("report_md"),
    }


def _print_summary(report: dict) -> None:
    gate = report.get("gate", {})
    summary = report.get("summary", {})
    run_meta = report.get("run", {})
    coverage = report.get("coverage", {})

    print(f"AstSight run: {run_meta.get('run_id')}")
    print(f"  模式: {run_meta.get('mode')}  门禁: {str(gate.get('verdict')).upper()}")
    print(f"  接口: 变更 {coverage.get('changed_endpoints', 0)} / 画像 "
          f"{coverage.get('profiled_endpoints', 0)} / 生成 "
          f"{coverage.get('generated_endpoints', 0)}")
    print(f"  用例: 生成 {summary.get('generated_cases', 0)} / 执行 "
          f"{summary.get('executed_cases', 0)} / 最终通过 "
          f"{summary.get('final_pass', 0)} ({summary.get('final_pass_rate', 0):.1%})")
    if summary.get("sut_defects"):
        print(f"  真实缺陷: {summary['sut_defects']}")
    store = report.get("case_store")
    if store:
        store_summary = store.get("summary") or {}
        req = store.get("requirement_coverage") or {}
        print(
            f"  用例库: v{store.get('store_version', 0)} / approved "
            f"{store_summary.get('approved', 0)} / deprecated "
            f"{store_summary.get('deprecated', 0)} / 待审 "
            f"{store.get('pending_proposals', 0)}"
        )
        print(
            f"  需求覆盖: {req.get('covered', 0)}/{req.get('total', 0)}"
            f" ({float(req.get('rate') or 0):.1%})"
        )
    for blocker in gate.get("blockers", []):
        print(f"  [BLOCK] {blocker}")
    for warning in gate.get("warnings", []):
        print(f"  [WARN] {warning}")
    artifacts = report.get("artifacts") or {}
    if artifacts.get("report_md"):
        print(f"  报告: {artifacts['report_md']}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
