"""M1 端到端：真实 git 分支 + 真实 FastAPI 服务 + 真实 pytest 执行。

不 mock 任何一层：
- 在临时目录建 git 仓库，base/feature 两个提交制造真实 diff；
- 用 uvicorn 启动仓库里的示例服务；
- 调用 CLI 同款 `pipeline.run`，断言受影响接口、生成、执行、归因与门禁。

不依赖 CodeGraph（`codegraph="off"`），保证 CI 可在无 Node 环境跑；
图谱路径由 `test_codegraph_embedded.py` 单独覆盖。
"""
from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from astsight.pipeline import RunConfig, run

DEMO_APP = Path(__file__).resolve().parents[1] / "examples" / "order_service" / "app.py"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _repo_with_branch(tmp_path: Path, mutate) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(DEMO_APP, repo / "app.py")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "e2e@example.com")
    _git(repo, "config", "user.name", "e2e")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature")

    path = repo / "app.py"
    source = path.read_text(encoding="utf-8")
    mutated = mutate(source)
    assert mutated != source, "mutation 未命中源码"
    path.write_text(mutated, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature change")
    return repo


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _service(repo: Path):
    port = _free_port()
    log = repo / "uvicorn.log"
    with open(log, "wb") as stream:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=repo, stdout=stream, stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("服务启动失败:\n" + log.read_text(encoding="utf-8", errors="replace")[-4000:])
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


def _config(repo: Path, base_url: str, **overrides) -> RunConfig:
    params = dict(
        project=str(repo),
        mode="delta",
        base="main",
        target="feature",
        base_url=base_url,
        reset_url=base_url + "/_reset",
        codegraph="off",
        retries=0,
        collect_only=True,
        timeout=180,
    )
    params.update(overrides)
    return RunConfig(**params)


def test_delta_finds_changed_handler_and_passes(tmp_path: Path) -> None:
    def refactor(source: str) -> str:
        return source.replace(
            "def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:\n"
            "    return _as_order(_order(order_id))",
            "def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:\n"
            "    order = _order(order_id)\n"
            "    return _as_order(order)",
        )

    repo = _repo_with_branch(tmp_path, refactor)
    with _service(repo) as base_url:
        report = run(_config(repo, base_url))

    changed = {api["handler"] for api in report["impact"]["changed_apis"]}
    assert changed == {"GET /api/v1/orders/{order_id}"}
    assert report["coverage"]["generated_endpoints"] == 1
    assert report["summary"]["generated_cases"] == 1
    assert report["summary"]["executed_cases"] == 1
    assert report["summary"]["final_pass"] == 1
    assert report["gate"]["verdict"] == "pass", report["gate"]
    assert (repo / ".astsight" / "runs" / report["run"]["run_id"] / "report.md").exists()


def test_delta_reports_real_defect_as_blocker(tmp_path: Path) -> None:
    def break_handler(source: str) -> str:
        return source.replace(
            "def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:\n"
            "    return _as_order(_order(order_id))",
            "def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:\n"
            "    raise HTTPException(status_code=500, detail=\"injected defect\")",
        )

    repo = _repo_with_branch(tmp_path, break_handler)
    with _service(repo) as base_url:
        report = run(_config(repo, base_url))

    assert report["summary"]["sut_defects"] == 1
    assert report["summary"]["final_pass"] == 0
    assert report["gate"]["verdict"] == "fail"
    assert report["failures"][0]["root_cause"] == "code_defect"
    assert report["failures"][0]["endpoint"] == "GET /api/v1/orders/{order_id}"


def test_full_mode_runs_all_demo_endpoints(tmp_path: Path) -> None:
    def noop(source: str) -> str:
        # full 模式不依赖 diff，但仍需要一个可提交的 feature 分支
        return source.replace(
            '"""AstSight 内置示例服务：订单中心（FastAPI）。',
            '"""AstSight 内置示例服务：订单中心（FastAPI）。\n\nfull 模式测试。',
            1,
        )

    repo = _repo_with_branch(tmp_path, noop)
    with _service(repo) as base_url:
        report = run(
            _config(
                repo, base_url, mode="full", allow_unmapped=True,
            )
        )

    assert report["coverage"]["changed_endpoints"] == 19
    assert report["coverage"]["generated_endpoints"] == 18
    assert report["summary"]["generated_cases"] == 39
    assert report["summary"]["executed_cases"] == 39
    assert report["summary"]["final_pass"] == 39, report["failures"]
    assert report["gate"]["verdict"] == "pass", report["gate"]
    # 契约缺口必须显式列出，而不是被 100% 掩盖
    assert "POST /_reset" in report["unmapped"]["contract_incomplete"]
