"""需求→审核用例→版本化→pytest 的端到端验证。

真实 FastAPI 服务、真实 OpenAPI、真实 pytest 执行；不 mock 生成/执行层。
"""
from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from astsight.cases.context import ContextError, prepare_context
from astsight.cases.draft import load_draft
from astsight.cases.proposal import approve_proposal, build_proposal, list_pending_proposals
from astsight.cases.store import empty_case_set, load_case_set, requirement_coverage
from astsight.cases.validate import validate_draft
from astsight.pipeline import RunConfig, run

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "order_service"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _service(project: Path):
    port = _free_port()
    log = project / "uvicorn.log"
    with open(log, "wb") as stream:
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
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "服务启动失败:\n"
                    + log.read_text(encoding="utf-8", errors="replace")[-4000:]
                )
            try:
                if httpx.get(base_url + "/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise RuntimeError("服务启动超时")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _project(tmp_path: Path) -> Path:
    target = tmp_path / "order_service"
    shutil.copytree(
        EXAMPLE,
        target,
        ignore=shutil.ignore_patterns("__pycache__", ".astsight", "uvicorn.log"),
    )
    return target


def _approve_all(proposal_path: Path, project: Path, store_path: Path) -> dict:
    review_path = proposal_path / "cases.review.json"
    data = json.loads(review_path.read_text(encoding="utf-8"))
    for item in data["items"]:
        item["decision"] = "approve"
        item["reviewer"] = "e2e"
        item["note"] = "sample review"
    review_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return approve_proposal(
        proposal_path=proposal_path,
        project=project,
        case_base=store_path,
        reviewer="e2e",
    )


def _config(project: Path, base_url: str, **overrides) -> RunConfig:
    params = dict(
        project=str(project),
        mode="cases",
        base_url=base_url,
        reset_url=base_url + "/_reset",
        codegraph="off",
        retries=0,
        collect_only=True,
        timeout=180,
    )
    params.update(overrides)
    return RunConfig(**params)


def test_requirement_cases_review_approve_and_execute(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store_path = project / "caseset.json"
    draft_path = project / "case_samples" / "cases.draft.json"

    with _service(project) as base_url:
        prepared = prepare_context(
            project,
            ["docs/api-design.md", "docs/requirements.md"],
            base_url=base_url,
        )
        context = prepared.context
        assert len(context["profiles"]) == 18
        assert any(
            item["section"] == "订单服务接口设计"
            for item in context["excluded_sections"]
        ) is False  # 父标题不是元数据，只作为叶子过滤，不进入覆盖率

        draft = load_draft(draft_path)
        validation = validate_draft(draft, context, project=project)
        assert not validation.errors, [item.model_dump() for item in validation.errors]
        # 未声明的另外 3 个接口 section 只 warning，不阻断
        assert any(item.code == "requirement.unaccounted" for item in validation.warnings)

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
        assert built.path is not None
        assert built.diff.counts()["new"] == 4
        assert not store_path.exists()

        # 导入不改变 store；待审 proposal 可被 status/run 发现
        pending = list_pending_proposals(project)
        assert len(pending) == 1

        approval = _approve_all(built.path, project, store_path)
        assert approval["store_version"] == 1
        approved = load_case_set(store_path)
        assert approved is not None
        assert len(approved.cases) == 4
        assert {entry.id for entry in approved.cases} == {
            "TC-0001",
            "TC-0002",
            "TC-0003",
            "TC-0004",
        }
        coverage = requirement_coverage(approved)
        assert coverage["total"] == 7
        assert coverage["covered"] == 2
        assert coverage["rate"] == 0.2857
        reasons = {
            (item["section"], item["reason"]) for item in coverage["uncovered"]
        }
        assert ("订单状态需求 > POST /api/v1/orders/{order_id}/pay", "needs_c5_state_setup") in reasons

        report = run(_config(project, base_url))
        assert report["gate"]["verdict"] == "pass", report["gate"]
        assert report["summary"]["generated_cases"] == 4
        assert report["summary"]["executed_cases"] == 4
        assert report["summary"]["final_pass"] == 4, report["failures"]
        state = report["case_store"]
        assert state["store_version"] == 1
        assert state["approved_by"] == "e2e"
        assert state["summary"]["approved"] == 4
        assert state["pending_proposals"] == 0
        assert state["requirement_coverage"]["covered"] == 2
        assert state["degraded"] is False, state
        assert state["contract_drift"]["checked"] is True
        assert state["contract_drift"]["changed"] is False

        run_dir = project / ".astsight" / "runs" / report["run"]["run_id"]
        assert (run_dir / "caseset.json").is_file()
        assert (run_dir / "requirement_coverage.json").is_file()
        assert (run_dir / "pending_proposals.json").is_file()
        assert (run_dir / "cases.json").is_file()
        report_md = (run_dir / "report.md").read_text(encoding="utf-8")
        assert "用例审核与版本" in report_md
        assert "cases" in report_md

        # 修改草案但不审核：旧 approved 版本继续生效，run 显示 pending/degraded
        data = json.loads(draft_path.read_text(encoding="utf-8"))
        data["cases"][0]["body"]["quantity"] = -1
        update_path = project / ".astsight" / "cases.draft.update.json"
        update_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        update = load_draft(update_path)
        updated_validation = validate_draft(update, context, project=project)
        assert not updated_validation.errors
        current = load_case_set(store_path)
        before = store_path.read_bytes()
        pending_proposal = build_proposal(
            project=project,
            draft_path=update_path,
            draft=update,
            context=context,
            validation=updated_validation,
            store=current,
            store_path=store_path,
        )
        assert pending_proposal.diff.counts()["update"] == 1
        assert store_path.read_bytes() == before
        assert len(list_pending_proposals(project)) == 1

        pending_report = run(_config(project, base_url))
        assert pending_report["case_store"]["pending_proposals"] == 1
        assert pending_report["case_store"]["degraded"] is True
        assert pending_report["case_store"]["store_version"] == 1
        assert any(
            "待审 proposal" in warning
            for warning in pending_report["gate"]["warnings"]
        )
        assert pending_report["summary"]["final_pass"] == 4
        assert pending_report["gate"]["verdict"] == "pass", pending_report["gate"]

        blocked = run(
            _config(
                project,
                base_url,
                fail_on_pending_proposals=True,
            )
        )
        assert blocked["gate"]["verdict"] == "fail"
        assert any(
            "--fail-on-pending-proposals" in blocker
            for blocker in blocked["gate"]["blockers"]
        )
        # 待审不影响本轮执行：仍然只跑上一版 approved
        assert blocked["summary"]["final_pass"] == 4


def test_prepare_rejects_missing_openapi(tmp_path: Path) -> None:
    project = tmp_path / "empty"
    project.mkdir()
    (project / "docs").mkdir()
    (project / "docs" / "api.md").write_text("# API\n\n## POST /x\n\nhello\n", encoding="utf-8")

    try:
        prepare_context(project, ["docs/api.md"])
    except ContextError as exc:
        assert "OpenAPI" in str(exc)
    else:
        raise AssertionError("缺少 OpenAPI 时必须失败，不能猜测字段")
