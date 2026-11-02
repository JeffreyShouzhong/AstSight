"""一条命令演示：规则 → Mock 语义 → 造数目录 → 用例 → 审核 → 造数计划 → 执行 → 诊断。

    export ASTSIGHT_LLM_API_KEY=...
    export ASTSIGHT_LLM_MODEL=...
    python examples/demo_agent_flow.py
    python examples/demo_agent_flow.py --keep --max-repair-rounds 1
    python examples/demo_agent_flow.py --keep --ddl schema.sql

脚本会：
1. 把 examples/downstream_service 复制到临时工作区；
2. 调真实模型抽取 BR 规则（CR 由 OpenAPI 确定性提取），生成 C3.5.1 Mock 语义；
3. 给了 `--ddl` 时先生成 C5a 数据目录（确定性结构 + 模型补语义）；
4. 调真实模型做 A4 两级用例设计，生成待审 proposal；
5. 以 demo 身份批准 new/update/revive（不批准 deprecate）；
6. 批准后用同一份已批准用例生成 C5b 造数计划；给了 DDL 时默认建临时 SQLite；
7. 启动内嵌 Mock，把 PRICING_BASE_URL 注入 SUT 后启动真实 uvicorn；
8. 执行用例、诊断失败；给了修复轮数时再用新 proposal 重跑。

没有模型 key 时会直接退出并提示环境变量，不会伪造成功。
"""
from __future__ import annotations

import argparse
import shutil
import socket
import sys
import tempfile
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from astsight.agent import FlowConfig, run_flow  # noqa: E402
from astsight.llm import LLMConfigError, config_from_env  # noqa: E402

EXAMPLE = Path(__file__).resolve().parent / "downstream_service"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时工作区与产物")
    parser.add_argument("--approve-as", default="demo")
    parser.add_argument(
        "--no-auto-approve", action="store_true", help="停在待审 proposal，不自动批准"
    )
    parser.add_argument("--max-repair-rounds", type=int, default=1)
    parser.add_argument("--mode", choices=("full", "delta"), default="full")
    parser.add_argument("--codegraph", choices=("auto", "off"), default="off")
    parser.add_argument(
        "--ddl",
        action="append",
        default=[],
        metavar="PATH",
        help="C5a DDL（相对示例项目，可重复）；提供后默认使用临时 SQLite",
    )
    parser.add_argument("--seed", default=None, help="C5a seed JSON（相对示例项目）")
    parser.add_argument(
        "--db-url", default=None, help="造数数据库 URL；缺省在临时工作区建 SQLite"
    )
    parser.add_argument("--skip-data", action="store_true", help="跳过 C5a/C5b")
    args = parser.parse_args()

    try:
        llm_config = config_from_env()
    except LLMConfigError as exc:
        print(f"模型未配置: {exc}", file=sys.stderr)
        print(
            "请先设置 ASTSIGHT_LLM_API_KEY 与 ASTSIGHT_LLM_MODEL；"
            "必要时设置 ASTSIGHT_LLM_PROVIDER / ASTSIGHT_LLM_BASE_URL。",
            file=sys.stderr,
        )
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="astsight-agent-"))
    project = workdir / "quote_service"
    shutil.copytree(EXAMPLE, project)
    port = _free_port()
    database_url = args.db_url
    if database_url is None and args.ddl and not args.skip_data:
        database_url = f"sqlite:///{(workdir / 'astsight-demo.db').as_posix()}"
    config = FlowConfig(
        project=str(project),
        requirements=["docs/requirements.md"],
        openapi=str(project / "openapi.json"),
        mode=args.mode,
        codegraph=args.codegraph,
        base_url=f"http://127.0.0.1:{port}",
        sut_command=[
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
        approve_as=None if args.no_auto_approve else args.approve_as,
        max_repair_rounds=max(0, args.max_repair_rounds),
        out=".astsight/agent",
        data_ddl=list(args.ddl),
        data_seed=args.seed,
        database_url=database_url,
        skip_data=args.skip_data,
    )
    print(f"工作区: {project}")
    print(
        f"模型: {llm_config.provider}/{llm_config.model} "
        f"base_url={llm_config.base_url}"
    )
    try:
        result = run_flow(config)
    except Exception as exc:
        print(f"流程失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.keep:
            print(f"保留工作区: {project}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
        return 1

    print(f"状态: {result.status}")
    for name, value in result.steps.items():
        print(f"  {name}: {value}")
    for warning in result.warnings:
        print(f"  [WARN] {warning}")
    for error in result.errors:
        print(f"  [ERROR] {error}")
    print(f"产物: {result.run_dir}")
    if result.report is not None:
        report = (result.report.get("artifacts") or {}).get("report_md")
        if report:
            print(f"报告: {report}")
    print(f"模型调用: {len(result.llm_usage)} 次")

    if args.keep:
        print(f"保留工作区: {project}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    if result.status == "pass":
        return 0
    return 2 if result.status in {"pending_review", "not_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
