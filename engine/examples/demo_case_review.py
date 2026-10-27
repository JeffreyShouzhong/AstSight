"""一条命令演示：需求 → 用例草案 → 人工审核 → 版本化 → pytest 接口用例。

    python examples/demo_case_review.py
    python examples/demo_case_review.py --keep
    python examples/demo_case_review.py --no-auto-approve   # 停在待审 proposal
    python examples/demo_case_review.py --codegraph off

脚本会：
1. 把 examples/order_service 复制到临时工作区，并启动真实 uvicorn；
2. `cases prepare`：需求文档 + OpenAPI → context/prompt/schema；
3. 使用 `case_samples/cases.draft.json` 作为 Skill/人工草案的样例；
4. `cases import`：校验并与已批准用例库 diff，只生成待审 proposal；
5. 默认以 demo 身份批准 new/update/revive（绝不自动批准 deprecate）；
6. `run --mode cases`：只执行已批准用例，打印门禁与报告路径。
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from astsight.cases.context import prepare_context  # noqa: E402
from astsight.cases.draft import load_draft  # noqa: E402
from astsight.cases.proposal import (  # noqa: E402
    approve_proposal,
    build_proposal,
    list_pending_proposals,
)
from astsight.cases.store import empty_case_set, load_case_set  # noqa: E402
from astsight.cases.validate import validate_draft  # noqa: E402
from astsight.pipeline import RunConfig, run  # noqa: E402

EXAMPLE = Path(__file__).resolve().parent / "order_service"
DOCS = ["docs/api-design.md", "docs/requirements.md"]
DRAFT = "case_samples/cases.draft.json"
_AUTO_APPROVE_ACTIONS = {"new", "update", "revive"}


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_project() -> Path:
    target = Path(tempfile.mkdtemp(prefix="astsight-cases-"))
    shutil.copytree(
        EXAMPLE,
        target / "order_service",
        ignore=shutil.ignore_patterns("__pycache__", ".astsight", "uvicorn.log"),
        dirs_exist_ok=True,
    )
    return target / "order_service"


def _wait_ready(base_url: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                "服务启动失败:\n"
                + log.read_text(encoding="utf-8", errors="replace")[-4000:]
            )
        try:
            if httpx.get(base_url + "/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.25)
    raise RuntimeError("服务启动超时")


def _fill_decisions(review_path: Path) -> None:
    """demo 专用：只自动批准新增/更新/恢复；废弃项显式 reject。"""
    review = json.loads(review_path.read_text(encoding="utf-8"))
    for item in review["items"]:
        if item["action"] in _AUTO_APPROVE_ACTIONS:
            item["decision"] = "approve"
            item["reviewer"] = "demo"
            item["note"] = "demo only: 自动批准，真实流程必须人工审核"
        elif item["action"] == "deprecate":
            item["decision"] = "reject"
            item["reviewer"] = "demo"
            item["note"] = "demo only: 绝不自动批准废弃"
        else:
            item["decision"] = "approve"
            item["reviewer"] = "demo"
    review_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(description="AstSight 需求→审核用例→执行演示")
    parser.add_argument("--keep", action="store_true", help="保留临时工作区与产物")
    parser.add_argument(
        "--no-auto-approve", action="store_true",
        help="不自动批准，停在待审 proposal；人工审完后执行 cases approve",
    )
    parser.add_argument(
        "--codegraph", choices=("auto", "off"), default="off",
        help="cases 模式不依赖 CodeGraph；保留参数与 demo_delta 保持一致",
    )
    args = parser.parse_args()

    project = _copy_project()
    store_path = project / "caseset.json"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = project / "uvicorn.log"
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=project,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    stop_for_review = False
    try:
        _wait_ready(base_url, proc, log_path)

        prepared = prepare_context(project, DOCS, base_url=base_url)
        context = prepared.context
        print(f"上下文包: {prepared.context_dir}")

        draft_path = project / DRAFT
        draft = load_draft(draft_path)
        validation = validate_draft(draft, context, project=project)
        if validation.errors:
            for issue in validation.errors:
                print(f"  [ERROR] {issue.code}: {issue.message}")
            raise RuntimeError("草案校验失败")
        for issue in validation.warnings:
            print(f"  [WARN] {issue.code}: {issue.message}")

        store = empty_case_set(project, context=context)
        built = build_proposal(
            project=project,
            draft_path=draft_path,
            draft=draft,
            context=context,
            validation=validation,
            store=store,
            store_path=store_path,
        )
        if built.path is None:
            print("无变更：草案与已批准用例库一致")
        else:
            counts = built.diff.counts()
            print(f"待审 proposal: {built.path}")
            print(
                "  diff: "
                + " ".join(f"{key}={value}" for key, value in counts.items() if value)
            )
            print(f"  待审提案数: {len(list_pending_proposals(project))}")

            if args.no_auto_approve:
                stop_for_review = True
                print("  已停在待审状态（工作区保留）。人工编辑后执行：")
                print(f"    审核文件: {built.path / 'cases.review.json'}")
                print(
                    f"    python -m astsight cases approve --project {project} "
                    f"--proposal {built.path} --reviewer <you>"
                )
                print(
                    f"    python -m astsight run --project {project} --mode cases "
                    f"--base-url http://127.0.0.1:<port>"
                )
            else:
                _fill_decisions(built.path / "cases.review.json")
                approval = approve_proposal(
                    proposal_path=built.path,
                    project=project,
                    case_base=store_path,
                    reviewer="demo",
                )
                approved = load_case_set(store_path)
                print(
                    f"已批准并写入 {store_path.name}: "
                    f"store v{approval['store_version']}, "
                    f"{len(approved.cases) if approved else 0} 条用例"
                )

        if not stop_for_review:
            report = run(
                RunConfig(
                    project=str(project),
                    mode="cases",
                    base_url=base_url,
                    reset_url=base_url + "/_reset",
                    case_base="caseset.json",
                    codegraph="off",
                    retries=0,
                    timeout=300,
                )
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    if stop_for_review:
        print("")
        print(f"工作区（已保留，供人工审核）: {project}")
        print("审核完成后用上面的 cases approve / run 命令继续；审核前不会执行任何草案用例。")
        return 0

    summary = report["summary"]
    gate = report["gate"]
    case_store = report.get("case_store") or {}
    print("")
    print(f"工作区: {project}")
    print(
        f"用例: 生成 {summary['generated_cases']} / 执行 {summary['executed_cases']} / "
        f"通过 {summary['final_pass']} / 真实缺陷 {summary['sut_defects']}"
    )
    if case_store:
        print(
            f"用例库: v{case_store.get('store_version')} / "
            f"approved {case_store.get('summary', {}).get('approved', 0)} / "
            f"待审 {case_store.get('pending_proposals', 0)} / "
            f"degraded {case_store.get('degraded')}"
        )
        coverage = case_store.get("requirement_coverage") or {}
        if coverage:
            print(
                f"需求覆盖: {coverage.get('covered')}/{coverage.get('total')} "
                f"({coverage.get('rate')})"
            )
    print(f"门禁: {gate['verdict'].upper()}")
    for blocker in gate["blockers"]:
        print(f"  [BLOCK] {blocker}")
    for warning in gate["warnings"]:
        print(f"  [WARN] {warning}")
    report_path = project / ".astsight" / "runs" / report["run"]["run_id"] / "report.md"
    suffix = "" if args.keep else "（临时目录将清理；加 --keep 保留）"
    print(f"报告: {report_path}{suffix}")

    if not args.keep:
        shutil.rmtree(project.parent, ignore_errors=True)
    return 0 if gate["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
