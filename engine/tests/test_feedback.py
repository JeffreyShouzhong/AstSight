"""本地诊断包与 GitHub Issue 反馈：脱敏、清单与链接。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from astsight.feedback import (
    collect_feedback,
    open_issue,
    scrub_headers,
    scrub_text,
    scrub_url,
)


def _run_dir(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    report = {
        "run": {
            "run_id": "r1",
            "mode": "delta",
            "project": str(project),
            "base_url": "https://api.internal/orders?token=abc123&q=1",
        },
        "summary": {
            "executed_cases": 2,
            "sut_defects": 1,
            "weak_oracle": ["TC-0001"],
            "unexecuted_cases": 0,
            "blocked_cases": 0,
        },
        "gate": {"verdict": "fail", "blockers": ["x"], "warnings": []},
        "failures": [
            {
                "case_id": "TC-0001",
                "endpoint": "POST /orders",
                "reason": "boom",
                "message": "token=abc123 email a@b.com phone 13800138000",
            }
        ],
        "audit": {"weak_oracle": {"checked": True, "count": 1}},
        "rules": {"id": "rs", "version": 1, "hash": "abc"},
        "data_plan": {"hash": "def"},
    }
    (run / "report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    (run / "execution.json").write_text(
        json.dumps(
            {
                "meta": {},
                "records": [
                    {
                        "case_id": "TC-0001",
                        "message": "Authorization: Bearer secret-token",
                        "diagnosis": {"reason": "x"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run / "mock_evidence.json").write_text(
        json.dumps(
            {
                "checked": True,
                "by_case": {"TC-0001": {"calls": 0}},
                "session_evidence": [
                    {
                        "case_id": "TC-0001",
                        "session_id": "s1",
                        "bindings": [
                            {
                                "dep_id": "http:svc:pay POST /pay",
                                "branch": "success",
                                "calls": 0,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run / "cases.json").write_text(
        json.dumps(
            [
                {
                    "id": "TC-0001",
                    "title": "order",
                    "endpoint": "POST /orders",
                    "category": "positive",
                    "transport": "http",
                    "assert_strength": "L1",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    generated = run / "generated"
    generated.mkdir()
    (generated / "test_orders.py").write_text(
        "def test_order():\n    # token=abc123\n    assert True\n",
        encoding="utf-8",
    )
    return run, project


def _zip_text(path: Path) -> str:
    with zipfile.ZipFile(path) as bundle:
        return "\n".join(
            bundle.read(name).decode("utf-8", errors="replace")
            for name in bundle.namelist()
            if not name.endswith(".zip")
        )


def test_collect_writes_scrubbed_bundle(tmp_path: Path) -> None:
    run, project = _run_dir(tmp_path)
    result = collect_feedback(run)
    target = Path(result["path"])
    assert target.is_file()
    names = {item["name"] for item in result["files"]}
    assert {
        "README.md",
        "summary.md",
        "diagnostic.json",
        "report.json",
        "execution.json",
        "mock_evidence.json",
        "cases/test_orders.py",
    } <= names
    text = _zip_text(target)
    for secret in (
        "secret-token",
        "abc123",
        "a@b.com",
        "13800138000",
        str(project),
    ):
        assert secret not in text
    assert "Authorization: ***" in text
    assert "token=***" in text
    assert "<email>" in text
    assert "<phone>" in text
    assert "https://<host>/orders?token=***&q=***" in text


def test_no_case_code_and_dry_run(tmp_path: Path) -> None:
    run, _project = _run_dir(tmp_path)
    preview = collect_feedback(run, dry_run=True)
    assert preview["dry_run"] is True
    assert not (run / "feedback.zip").exists()
    assert any(item["name"] == "diagnostic.json" for item in preview["files"])

    result = collect_feedback(
        run, out=tmp_path / "no-code.zip", include_case_code=False
    )
    names = {item["name"] for item in result["files"]}
    assert not any(name.startswith("cases/") for name in names)


def test_open_issue_writes_draft_and_link(tmp_path: Path) -> None:
    run, _project = _run_dir(tmp_path)
    result = open_issue(run, repo="acme/astsight")
    issue = Path(result["issue_md"])
    assert issue.is_file()
    text = issue.read_text(encoding="utf-8")
    assert "## 环境" in text
    assert "## 现象" in text
    assert "feedback.zip" in text
    assert result["url"].startswith(
        "https://github.com/acme/astsight/issues/new?"
    )
    assert "labels=framework-defect" in result["url"]


def test_cli_feedback_commands(tmp_path: Path) -> None:
    from astsight.cli import main

    run, _project = _run_dir(tmp_path)
    assert main(["feedback", "collect", "--run", str(run)]) == 0
    assert (run / "feedback.zip").is_file()
    assert (
        main(
            [
                "feedback",
                "open",
                "--run",
                str(run),
                "--repo",
                "acme/astsight",
            ]
        )
        == 0
    )
    assert (run / "issue.md").is_file()


def test_emit_bundle_respects_feedback_mode(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from astsight.pipeline import _emit_feedback_bundle

    run, _project = _run_dir(tmp_path)
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))

    bundle = _emit_feedback_bundle(
        SimpleNamespace(feedback="auto"), run, report
    )
    assert bundle is not None
    assert Path(bundle["path"]).is_file()

    passed = {**report, "gate": {"verdict": "pass"}}
    assert (
        _emit_feedback_bundle(SimpleNamespace(feedback="auto"), run, passed)
        is None
    )
    assert _emit_feedback_bundle(
        SimpleNamespace(feedback="always"), run, passed
    ) is not None
    assert (
        _emit_feedback_bundle(SimpleNamespace(feedback="off"), run, report)
        is None
    )


def test_collect_uses_in_memory_report(tmp_path: Path) -> None:
    run, _project = _run_dir(tmp_path)
    (run / "report.json").unlink()
    report = json.loads(
        json.dumps(
            {
                "run": {"run_id": "r2", "mode": "cases"},
                "summary": {"executed_cases": 0, "sut_defects": 0},
                "gate": {"verdict": "fail", "warnings": []},
                "failures": [],
            }
        )
    )
    result = collect_feedback(run, report=report)
    assert Path(result["path"]).is_file()
    names = {item["name"] for item in result["files"]}
    assert "diagnostic.json" in names


def test_scrub_helpers() -> None:
    assert scrub_text("token=abc password=xyz") == "token=*** password=***"
    assert scrub_text("a@b.com 13800138000") == "<email> <phone>"
    assert (
        scrub_url("https://user:pass@host:8443/x?token=abc&q=1")
        == "https://<host>:8443/x?token=***&q=***"
    )
    headers = scrub_headers(
        {
            "Authorization": "Bearer abc",
            "Content-Type": "application/json",
            "X-Trace": "abc",
        }
    )
    assert headers["Authorization"] == "***"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Trace"] == "***"
