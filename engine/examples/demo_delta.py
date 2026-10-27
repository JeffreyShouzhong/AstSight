"""一条命令演示 M1 delta 闭环（真实 git + 真实服务 + 真实 pytest）。

    python examples/demo_delta.py            # 正常改动：全绿
    python examples/demo_delta.py --defect   # 注入 500：报告门禁 FAIL
    python examples/demo_delta.py --keep     # 保留临时仓库与产物，便于查看

脚本会：
1. 把 examples/order_service/app.py 复制到临时目录并 git init；
2. 在 main 提交基线，在 feature 分支提交一次真实改动；
3. 用 uvicorn 启动改动后的服务；
4. 执行 `git diff → 影响接口 → OpenAPI 画像 → 模板用例 → pytest → 报告`；
5. 打印门禁结论与报告路径。
"""
from __future__ import annotations

import argparse
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

from astsight.pipeline import RunConfig, run  # noqa: E402

APP = Path(__file__).resolve().parent / "order_service" / "app.py"

_REFACTOR = (
    "def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:\n"
    "    return _as_order(_order(order_id))"
)
_REFACTORED = (
    "def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:\n"
    "    # demo: 行为不变的内部改写，用于验证变更感知\n"
    "    order = _order(order_id)\n"
    "    return _as_order(order)"
)
_DEFECT = (
    "def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:\n"
    "    # demo: 故意注入 500，验证报告能识别真实缺陷\n"
    "    raise HTTPException(status_code=500, detail=\"injected defect\")"
)


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_repo(defect: bool) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="astsight-demo-"))
    shutil.copy(APP, repo / "app.py")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "demo@astsight.local")
    _git(repo, "config", "user.name", "AstSight Demo")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature")

    app = repo / "app.py"
    source = app.read_text(encoding="utf-8")
    if _REFACTOR not in source:
        raise RuntimeError("示例服务与 demo 脚本不匹配：未找到 get_order 基线代码")
    app.write_text(source.replace(_REFACTOR, _DEFECT if defect else _REFACTORED), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "inject defect" if defect else "refactor get_order")
    return repo


def _wait_ready(base_url: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务启动失败:\n" + log.read_text(encoding="utf-8", errors="replace")[-4000:])
        try:
            if httpx.get(base_url + "/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.25)
    raise RuntimeError("服务启动超时")


def main() -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(description="AstSight M1 delta 演示")
    parser.add_argument("--defect", action="store_true", help="注入 500 缺陷，演示门禁失败")
    parser.add_argument("--keep", action="store_true", help="保留临时仓库与产物")
    parser.add_argument("--codegraph", choices=("auto", "off"), default="auto")
    args = parser.parse_args()

    repo = _build_repo(args.defect)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = repo / "uvicorn.log"
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=repo, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        _wait_ready(base_url, proc, log_path)
        report = run(
            RunConfig(
                project=str(repo),
                mode="delta",
                base="main",
                target="feature",
                base_url=base_url,
                reset_url=base_url + "/_reset",
                codegraph=args.codegraph,
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

    summary = report["summary"]
    gate = report["gate"]
    print("")
    print(f"临时仓库: {repo}")
    print(f"受影响接口: {', '.join(a['handler'] for a in report['impact']['changed_apis'])}")
    print(f"用例: 生成 {summary['generated_cases']} / 执行 {summary['executed_cases']} / "
          f"通过 {summary['final_pass']} / 真实缺陷 {summary['sut_defects']}")
    print(f"门禁: {gate['verdict'].upper()}")
    for blocker in gate["blockers"]:
        print(f"  [BLOCK] {blocker}")
    for warning in gate["warnings"]:
        print(f"  [WARN] {warning}")
    report_path = repo / ".astsight" / "runs" / report["run"]["run_id"] / "report.md"
    suffix = "" if args.keep else "（临时目录将清理；加 --keep 保留）"
    print(f"报告: {report_path}{suffix}")

    if not args.keep:
        shutil.rmtree(repo, ignore_errors=True)
    return 0 if gate["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
