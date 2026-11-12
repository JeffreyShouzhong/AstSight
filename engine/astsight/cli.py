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
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codegraph import (
    CODEGRAPH_VERSION,
    install as install_codegraph_runtime,
    status as codegraph_runtime_status,
)
from contracts import DataCatalog, DataPlanBundle, MockPlan, RuleSet

from .cases.context import load_context, prepare_context
from .cases.draft import load_draft
from .cases.proposal import (
    ProposalError,
    approve_proposal,
    build_proposal,
    classify,
    interactive_review,
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
from .data import (
    SeedData,
    apply_bundle,
    apply_semantics,
    build_catalog,
    build_data_plan_bundle,
    load_seed,
    parse_ddl,
    validate_catalog,
)
from .data.semantics import SemanticDraft
from .inventory import discover_openapi, inventory_routes
from .mock import MockPlanError, validate_mock_plan
from .mock.semantic import (
    apply_mock_semantic_candidates,
    build_mock_semantic,
    validate_mock_semantic,
)
from .persistence import load_json, save_json_atomic
from .pipeline import RunConfig, run
from .profile import build_plan_from_profiles, build_profiles, load_spec, parse_operations
from .rules import (
    RuleDraft,
    RuleStoreError,
    build_rules_context,
    import_rule_draft,
    load_rule_set,
    load_rules_context,
    load_rule_draft,
    prepare_rules_context,
    render_rule_set_markdown,
    rule_coverage,
    rule_set_hash,
    rule_set_snapshot,
    save_rule_set,
    write_rules_context,
)

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
    run_parser.add_argument(
        "--data-plan", default=None,
        help="C5b data_plan.json；有前置状态的用例按用例 apply/teardown",
    )
    run_parser.add_argument(
        "--db-url", default=None,
        help="造数数据库 URL（sqlite:///、postgresql:// 或 mysql://）；"
        "缺省时相关用例标 blocked",
    )
    run_parser.add_argument(
        "--ruleset", default=None,
        help="规则库 JSON；缺省读取项目 ruleset.json（存在时）",
    )
    run_parser.add_argument(
        "--allow-stale-rules", action="store_true",
        help="规则库 hash 与 caseset 快照不一致时降级为 warning",
    )
    run_parser.add_argument(
        "--mock-plan", default=None,
        help="Mock 计划 JSON；启动内嵌 Mock 服务并自动注入 admin 地址/token",
    )
    run_parser.add_argument(
        "--mock-admin-url", default=None,
        help="已启动的 HTTP Mock 管理地址；提供后用用例级会话执行",
    )
    run_parser.add_argument(
        "--mock-token", default=None,
        help="Mock 管理 token；默认读取 ASTSIGHT_MOCK_TOKEN",
    )
    run_parser.add_argument("--mock-namespace", default=None)
    run_parser.add_argument("--mock-run-scope", default=None)
    run_parser.add_argument("--mock-drain-ms", type=int, default=None)
    run_parser.add_argument("--mock-ttl-ms", type=int, default=None)
    run_parser.add_argument(
        "--mock-evidence", default=None,
        help="mock_calls.jsonl 路径；缺省从 --mock-admin-url 的 /_admin/server 获取",
    )
    run_parser.add_argument("--json", action="store_true", help="把报告摘要打印为 JSON")

    cases_parser = sub.add_parser("cases", help="需求→用例上下文、导入、审核与用例库")
    cases_sub = cases_parser.add_subparsers(dest="cases_command", required=True)

    prepare = cases_sub.add_parser("prepare", help="生成 context/prompt/schema 上下文包")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--requirements", action="append", required=True, metavar="PATH")
    prepare.add_argument("--openapi", default=None)
    prepare.add_argument("--base-url", default=None)
    prepare.add_argument(
        "--codegraph",
        choices=("auto", "on", "off"),
        default="auto",
        help="扫描代码路由所用的图谱模式（有 OpenAPI 时也会用真实 handler 补下游）",
    )
    prepare.add_argument(
        "--no-scan-routes",
        action="store_true",
        help="不扫描代码路由；只使用 OpenAPI/合成路由生成上下文",
    )
    prepare.add_argument(
        "--data-catalog", default=None, help="数据目录 JSON；随上下文打包供 A4 使用"
    )
    prepare.add_argument(
        "--mock-semantics", default=None, help="Mock 语义 JSON；随上下文打包供 A4 使用"
    )
    prepare.add_argument("--out", default=".astsight/casegen", help="相对 project")
    prepare.add_argument("--json", action="store_true")

    imp = cases_sub.add_parser("import", help="校验草案并生成待审 proposal")
    imp.add_argument("--project", required=True)
    imp.add_argument("--context", required=True, help="context.json 或其目录")
    imp.add_argument("--draft", required=True)
    imp.add_argument("--case-base", default=None)
    imp.add_argument("--ruleset", default=None, help="规则库 JSON；用于校验 rule_refs")
    imp.add_argument(
        "--data-catalog", default=None, help="数据目录 JSON；用于校验 preconditions"
    )
    imp.add_argument(
        "--mock-semantics", default=None, help="Mock 语义 JSON；用于校验 mock_bindings"
    )
    imp.add_argument(
        "--require-rules", action="store_true", help="强制每条用例引用规则"
    )
    imp.add_argument("--dry-run", action="store_true", help="只校验/diff，不写 proposal")
    imp.add_argument("--json", action="store_true")

    approve = cases_sub.add_parser("approve", help="按审核文件把批准项写入 caseset.json")
    approve.add_argument("--project", required=True)
    approve.add_argument("--proposal", required=True, help="proposal 目录或 proposal.json")
    approve.add_argument("--case-base", default=None)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--json", action="store_true")

    review = cases_sub.add_parser(
        "review", help="交互式填写审核决定（只改 cases.review.json，不批准）"
    )
    review.add_argument("--proposal", required=True, help="proposal 目录或 proposal.json")
    review.add_argument(
        "--pending", action="store_true", help="只处理仍为 pending 的条目"
    )
    review.add_argument("--json", action="store_true")

    status = cases_sub.add_parser("status", help="查看用例库/待审/需求覆盖")
    status.add_argument("--project", required=True)
    status.add_argument("--case-base", default=None)
    status.add_argument("--json", action="store_true")

    mock_parser = sub.add_parser("mock", help="C3.5.5 下游 Mock 计划")
    mock_sub = mock_parser.add_subparsers(dest="mock_command", required=True)
    plan = mock_sub.add_parser(
        "plan", help="从 C3 下游调用点生成 MockPlan 草案（不自动补语义分支）"
    )
    plan.add_argument("--project", required=True)
    plan.add_argument("--openapi", default=None, help="OpenAPI 文件或 URL；缺省自动发现")
    plan.add_argument(
        "--out",
        default=None,
        help="输出路径；相对路径按 project 解析（默认 .astsight/mock-plan.json）",
    )
    plan.add_argument("--sut-id", default=None, help="缺省取项目目录名")
    plan.add_argument("--host", default="127.0.0.1")
    plan.add_argument("--port", type=int, default=0)
    plan.add_argument("--codegraph", choices=("auto", "on", "off"), default="auto")
    plan.add_argument("--codegraph-module-root", default=None)
    plan.add_argument("--full-index", action="store_true")
    plan.add_argument("--force", action="store_true", help="允许覆盖已存在的计划文件")
    plan.add_argument("--json", action="store_true")

    mock_semantics = mock_sub.add_parser(
        "semantics",
        help="C3.5.1：画像下游 + 规则库 → mock_semantics.json（确定性 + 可选 LLM 候选）",
    )
    mock_semantics.add_argument("--project", required=True)
    mock_semantics.add_argument(
        "--context", required=True, help="casegen context.json 或其目录"
    )
    mock_semantics.add_argument(
        "--ruleset", default=None, help="规则库 JSON；用于规则映射与候选校验"
    )
    mock_semantics.add_argument(
        "--draft", default=None, help="模型在框架外产出的候选 JSON（与 --llm 二选一）"
    )
    mock_semantics.add_argument(
        "--llm", action="store_true", help="调用模型补齐 success 响应体/有依据的分支"
    )
    mock_semantics.add_argument("--prompt", default=None, help="覆盖 C3.5.1 提示词路径")
    mock_semantics.add_argument(
        "--llm-config",
        default=None,
        metavar="PATH",
        help="模型配置文件（默认 ./llm.json 或 ~/.astsight/llm.json；"
        "也可用 ASTSIGHT_LLM_CONFIG）",
    )
    mock_semantics.add_argument(
        "--out", default=None, help="默认 .astsight/mock_semantics.json（相对 project）"
    )
    mock_semantics.add_argument("--json", action="store_true")

    rules_parser = sub.add_parser(
        "rules", help="规则库：需求文档抽取、框架外导入、状态与评审文档"
    )
    rules_sub = rules_parser.add_subparsers(dest="rules_command", required=True)

    rules_extract = rules_sub.add_parser(
        "extract",
        aliases=["c1"],
        help="C1：需求+设计（可选）+OpenAPI（可选）→ ruleset.json + ruleset.md（调用模型）",
    )
    rules_extract.add_argument("--project", required=True)
    rules_extract.add_argument(
        "--requirements", action="append", required=True, metavar="PATH"
    )
    rules_extract.add_argument(
        "--design",
        action="append",
        default=[],
        metavar="PATH",
        help="可选：设计文档，可重复；与 --requirements 一起作为 A1 输入",
    )
    rules_extract.add_argument(
        "--openapi",
        default=None,
        help="可选：补充 CR 契约规则；不传时自动发现项目内 openapi 文件，仍无则只生成 BR",
    )
    rules_extract.add_argument("--out", default=".astsight/rules", help="相对 project")
    rules_extract.add_argument("--rule-base", default=None, help="相对 project")
    rules_extract.add_argument(
        "--max-rules", type=int, default=500, help="单次抽取规则上限（<=500）"
    )
    rules_extract.add_argument(
        "--prompt", default=None, help="覆盖 A1 提示词路径（默认 prompts/a1_requirement_analysis.md）"
    )
    rules_extract.add_argument(
        "--llm-config",
        default=None,
        metavar="PATH",
        help="模型配置文件（默认 ./llm.json 或 ~/.astsight/llm.json；"
        "也可用 ASTSIGHT_LLM_CONFIG）",
    )
    rules_extract.add_argument("--dry-run", action="store_true", help="只校验，不写规则库")
    rules_extract.add_argument("--json", action="store_true")

    rules_prepare = rules_sub.add_parser(
        "prepare", help="仅生成 A1 上下文包（模型在框架外运行时使用）"
    )
    rules_prepare.add_argument("--project", required=True)
    rules_prepare.add_argument(
        "--requirements", action="append", required=True, metavar="PATH"
    )
    rules_prepare.add_argument(
        "--design",
        action="append",
        default=[],
        metavar="PATH",
        help="可选：设计文档，可重复；与 --requirements 一起生成上下文",
    )
    rules_prepare.add_argument("--openapi", default=None)
    rules_prepare.add_argument("--out", default=".astsight/rules", help="相对 project")
    rules_prepare.add_argument(
        "--prompt", default=None, help="覆盖 A1 提示词路径（默认 prompts/a1_requirement_analysis.md）"
    )
    rules_prepare.add_argument("--json", action="store_true")

    rules_import = rules_sub.add_parser(
        "import", help="校验 A1 草案并原子写入 ruleset.json"
    )
    rules_import.add_argument("--project", required=True)
    rules_import.add_argument("--context", required=True, help="rules context 目录")
    rules_import.add_argument("--draft", required=True)
    rules_import.add_argument("--rule-base", default=None, help="相对 project")
    rules_import.add_argument("--dry-run", action="store_true")
    rules_import.add_argument("--json", action="store_true")

    rules_status = rules_sub.add_parser("status", help="查看规则库、覆盖与漂移")
    rules_status.add_argument("--project", required=True)
    rules_status.add_argument("--rule-base", default=None)
    rules_status.add_argument("--case-base", default=None)
    rules_status.add_argument("--json", action="store_true")

    rules_render = rules_sub.add_parser(
        "render", help="把 ruleset.json 渲染成人工评审用的 Markdown"
    )
    rules_render.add_argument("--project", required=True)
    rules_render.add_argument("--rule-base", default=None)
    rules_render.add_argument(
        "--out", default=None, help="默认写到 ruleset.json 同目录的 ruleset.md"
    )
    rules_render.add_argument("--stdout", action="store_true", help="直接打印，不写文件")
    rules_render.add_argument("--json", action="store_true")

    data_parser = sub.add_parser("data", help="C5 数据目录、造数计划与执行")
    data_sub = data_parser.add_subparsers(dest="data_command", required=True)

    data_catalog = data_sub.add_parser("catalog", help="DDL/seed → data_catalog.json")
    data_catalog.add_argument("--project", required=True)
    data_catalog.add_argument("--ddl", action="append", required=True, metavar="PATH")
    data_catalog.add_argument("--seed", default=None)
    data_catalog.add_argument(
        "--dialect",
        default="sqlite",
        choices=("sqlite", "postgres", "mysql"),
        help="DDL 与造数 SQL 方言（默认 sqlite）",
    )
    data_catalog.add_argument("--context", default=None, help="rules context 目录/JSON")
    data_catalog.add_argument(
        "--out", default=None, help="输出文件或目录（默认 .astsight/data）"
    )
    data_catalog.add_argument("--json", action="store_true")

    data_semantics = data_sub.add_parser(
        "semantics", help="合并 LLM 字段语义/业务态候选（逐项校验）"
    )
    data_semantics.add_argument("--project", required=True)
    data_semantics.add_argument("--catalog", required=True)
    data_semantics.add_argument(
        "--draft", default=None, help="模型在框架外产出的语义草案（与 --llm 二选一）"
    )
    data_semantics.add_argument(
        "--llm", action="store_true", help="调用模型生成字段语义/业务态候选"
    )
    data_semantics.add_argument(
        "--context", default=None, help="casegen/rules context；--llm 时提供需求原文"
    )
    data_semantics.add_argument("--prompt", default=None, help="覆盖 C5a 提示词路径")
    data_semantics.add_argument(
        "--llm-config",
        default=None,
        metavar="PATH",
        help="模型配置文件（默认 ./llm.json 或 ~/.astsight/llm.json；"
        "也可用 ASTSIGHT_LLM_CONFIG）",
    )
    data_semantics.add_argument("--ruleset", default=None)
    data_semantics.add_argument("--out", default=None)
    data_semantics.add_argument("--json", action="store_true")

    data_plan = data_sub.add_parser(
        "plan", help="approved cases + catalog → data_plan.json"
    )
    data_plan.add_argument("--project", required=True)
    data_plan.add_argument("--catalog", required=True)
    data_plan.add_argument("--cases", default=None)
    data_plan.add_argument("--ruleset", default=None)
    data_plan.add_argument("--fixtures", default=None, help="适配层造数候选 JSON")
    data_plan.add_argument("--run-scope", default="")
    data_plan.add_argument("--out", default=None)
    data_plan.add_argument("--json", action="store_true")

    data_apply = data_sub.add_parser(
        "apply", help="在 SQLite/PostgreSQL/MySQL 中造数/回收"
    )
    data_apply.add_argument(
        "--db-url",
        required=True,
        help="sqlite:///path、postgresql://user:pass@host/db 或 mysql://user:pass@host/db",
    )
    data_apply.add_argument("--plan", required=True)
    data_apply.add_argument("--run-scope", default=None)
    data_apply.add_argument(
        "--keep-data", action="store_true", help="保留数据；默认执行后立即回收"
    )
    data_apply.add_argument("--json", action="store_true")

    codegraph_parser = sub.add_parser(
        "codegraph", help="CodeGraph 运行时：持久化安装与状态"
    )
    codegraph_sub = codegraph_parser.add_subparsers(
        dest="codegraph_command", required=True
    )
    codegraph_install = codegraph_sub.add_parser(
        "install", help="安装到用户级缓存，所有项目自动复用"
    )
    codegraph_install.add_argument(
        "--version", default=CODEGRAPH_VERSION, help="CodeGraph 版本（默认固定版本）"
    )
    codegraph_install.add_argument("--root", default=None, help="覆盖安装目录")
    codegraph_install.add_argument(
        "--force", action="store_true", help="已安装也重新安装"
    )
    codegraph_install.add_argument("--json", action="store_true")
    codegraph_status = codegraph_sub.add_parser("status", help="查看运行时定位结果")
    codegraph_status.add_argument("--root", default=None, help="覆盖安装目录")
    codegraph_status.add_argument("--json", action="store_true")

    agent_parser = sub.add_parser(
        "agent", help="LLM 辅助端到端流程（规则→Mock 语义→造数目录→用例→计划→执行→诊断）"
    )
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_run = agent_sub.add_parser(
        "run", help="从代码变更跑到自我修复；模型配置读 llm.json 或环境变量"
    )
    agent_run.add_argument("--project", required=True)
    agent_run.add_argument("--requirements", action="append", required=True, metavar="PATH")
    agent_run.add_argument("--openapi", default=None, help="本地 OpenAPI；缺省自动发现")
    agent_run.add_argument(
        "--llm-config",
        default=None,
        metavar="PATH",
        help="模型配置文件（默认 ./llm.json 或 ~/.astsight/llm.json；"
        "也可用 ASTSIGHT_LLM_CONFIG）",
    )
    agent_run.add_argument("--mode", choices=("full", "delta"), default="full")
    agent_run.add_argument("--base", default="main")
    agent_run.add_argument("--target", default="HEAD")
    agent_run.add_argument("--depth", type=int, default=5)
    agent_run.add_argument("--base-url", default=None, help="SUT 地址；不提供则只生成不执行")
    agent_run.add_argument("--reset-url", default=None)
    agent_run.add_argument("--codegraph", choices=("auto", "on", "off"), default="auto")
    agent_run.add_argument("--codegraph-module-root", default=None)
    agent_run.add_argument("--full-index", action="store_true")
    agent_run.add_argument("--case-base", default=None)
    agent_run.add_argument(
        "--rule-base", default=None, help="项目级规则库路径（默认 ruleset.json）"
    )
    agent_run.add_argument(
        "--ddl", action="append", default=[], metavar="PATH",
        help="C5a 数据库 DDL（相对 project，可重复）",
    )
    agent_run.add_argument("--seed", default=None, help="C5a seed JSON（相对 project）")
    agent_run.add_argument(
        "--db-dialect",
        default=None,
        choices=("sqlite", "postgres", "mysql"),
        help="C5a 造数 SQL 方言；缺省从 --db-url 推断，否则 sqlite",
    )
    agent_run.add_argument(
        "--data-catalog", default=None, help="复用已有 data_catalog.json"
    )
    agent_run.add_argument(
        "--data-plan", default=None, help="复用已有 data_plan.json（跳过 C5b LLM）"
    )
    agent_run.add_argument(
        "--db-url",
        default=None,
        help="造数数据库 URL（sqlite:///、postgresql:// 或 mysql://）",
    )
    agent_run.add_argument(
        "--skip-data", action="store_true", help="跳过 C5 数据目录与造数计划"
    )
    agent_run.add_argument("--out", default=".astsight/agent")
    agent_run.add_argument(
        "--approve-as",
        default=None,
        metavar="REVIEWER",
        help="以该审核人身份批准 new/update/revive；deprecate 永远拒绝。"
        "不提供则停在待审 proposal",
    )
    agent_run.add_argument(
        "--max-repair-rounds",
        type=int,
        default=3,
        help="诊断/修复后重跑的最大轮数（默认 3；0 关闭自动修复）",
    )
    agent_run.add_argument("--max-cases", type=int, default=200)
    agent_run.add_argument(
        "--sut-command",
        default=None,
        help="启动 SUT 的命令行，按空白切分、不经过 shell；需配合 --base-url",
    )
    agent_run.add_argument("--sut-ready-url", default=None)
    agent_run.add_argument(
        "--sut-env", action="append", default=[], metavar="KEY=VALUE",
        help="额外传给 SUT 的环境变量（可重复）",
    )
    agent_run.add_argument("--sut-timeout", type=float, default=60.0)
    agent_run.add_argument("--timeout", type=float, default=600.0)
    agent_run.add_argument("--retries", type=int, default=1)
    agent_run.add_argument("--no-collect-check", action="store_true")
    agent_run.add_argument("--skip-execute", action="store_true")
    agent_run.add_argument("--strict-unmapped", action="store_true")
    agent_run.add_argument("--min-pass-rate", type=float, default=0.95)
    agent_run.add_argument("--mock-host", default="127.0.0.1")
    agent_run.add_argument("--mock-port", type=int, default=0)
    agent_run.add_argument("--json", action="store_true")
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
        data_plan=args.data_plan,
        database_url=args.db_url,
        ruleset=args.ruleset,
        allow_stale_rules=args.allow_stale_rules,
        mock_plan=args.mock_plan,
        mock_admin_url=args.mock_admin_url,
        mock_token=args.mock_token,
        mock_namespace=args.mock_namespace,
        mock_run_scope=args.mock_run_scope,
        mock_drain_ms=args.mock_drain_ms,
        mock_ttl_ms=args.mock_ttl_ms,
        mock_evidence=args.mock_evidence,
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
        catalog = _load_optional_catalog(project, args.data_catalog)
        mock_semantic = _load_optional_mock_semantic(project, args.mock_semantics)
        prepared = prepare_context(
            project,
            list(args.requirements),
            openapi=args.openapi,
            base_url=args.base_url,
            out=args.out,
            scan_routes=not args.no_scan_routes,
            codegraph=args.codegraph,
            data_catalog=catalog,
            mock_semantic=mock_semantic,
        )
    except Exception as exc:
        print(f"cases prepare 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    context = prepared.context
    payload = {
        "context_id": prepared.context_id,
        "context_dir": str(prepared.context_dir),
        "context": str(prepared.context_path),
        "prompt": str(prepared.prompt_path),
        "schema": str(prepared.schema_path),
        "endpoints": len(context.get("profiles") or []),
        "documents": len(context.get("documents") or []),
        "openapi": str((context.get("openapi") or {}).get("source") or ""),
        "warnings": context.get("warnings") or [],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _resolve_under_project(project: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        cwd_candidate = (Path.cwd() / path).resolve()
        path = cwd_candidate if cwd_candidate.exists() else (project / path).resolve()
    return path.resolve()


def _load_optional_ruleset(project: Path, raw: str | None) -> RuleSet | None:
    path = _resolve_under_project(project, raw)
    if path is None or not path.is_file():
        return None
    return load_rule_set(path)


def _load_optional_catalog(project: Path, raw: str | None) -> DataCatalog | None:
    path = _resolve_under_project(project, raw)
    if path is None or not path.is_file():
        return None
    return DataCatalog.model_validate(load_json(path))


def _load_optional_mock_semantic(project: Path, raw: str | None):
    from contracts import MockSemantic

    path = _resolve_under_project(project, raw)
    if path is None or not path.is_file():
        return None
    return MockSemantic.model_validate(load_json(path))


def _cases_import(args) -> int:
    project = Path(args.project).resolve()
    try:
        context = load_context(args.context)
        draft = load_draft(args.draft)
        ruleset = _load_optional_ruleset(project, args.ruleset)
        catalog = _load_optional_catalog(project, args.data_catalog)
        if catalog is None and context.get("data_catalog"):
            catalog = DataCatalog.model_validate(context["data_catalog"])
        mock_semantic = _load_optional_mock_semantic(project, args.mock_semantics)
        if mock_semantic is None and context.get("mock_semantics"):
            from contracts import MockSemantic

            mock_semantic = MockSemantic.model_validate(context["mock_semantics"])
        validation = validate_draft(
            draft,
            context,
            project=project,
            ruleset=ruleset,
            data_catalog=catalog,
            mock_semantic=mock_semantic,
            require_rules=args.require_rules,
        )
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
            ruleset=ruleset,
            data_catalog=catalog,
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


def _cases_review(args) -> int:
    try:
        result = interactive_review(
            args.proposal, only_pending=args.pending
        )
    except ProposalError as exc:
        print(f"cases review 拒绝: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"cases review 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
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


def _rules_llm(config_path: str | None = None):
    """按配置文件 + 环境变量构造模型客户端；测试通过 monkeypatch 替换。"""
    from .llm import LLMClient, load_config

    return LLMClient(load_config(config_path=config_path))


def _rule_source_documents(args) -> list[str]:
    """需求文档必填，设计文档可选；两者都作为 A1 的原文来源。"""
    return [*args.requirements, *getattr(args, "design", [])]


def _rules_extract(args) -> int:
    """需求文档是入口：确定性上下文 + 模型抽规则 + 确定性导入，一次完成。

    模型只产出候选草案；`rule_key`、A 级落地、冲突检测、CR 契约规则与
    版本号都由框架重算。校验不通过时 ruleset.json 保持原样。
    """
    from .agent.rule_analysis import generate_rule_draft, rule_prompt_metadata

    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        context = build_rules_context(
            project,
            _rule_source_documents(args),
            openapi=args.openapi,
            prompt_path=args.prompt,
        )
        prepared = write_rules_context(
            context, project=project, out=args.out, prompt_path=args.prompt
        )
    except Exception as exc:
        print(f"rules extract 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    draft_path = prepared.context_dir / "rules.draft.json"
    llm = None
    try:
        llm = _rules_llm(getattr(args, "llm_config", None))
        draft = generate_rule_draft(
            context,
            llm=llm,
            prompt_path=args.prompt,
            max_rules=max(1, min(500, args.max_rules)),
        )
        # 草案先落盘再校验：校验失败时保留模型原始输出，便于定位是模型问题
        # 还是需求本身不可判定；规则库本身不写。
        save_json_atomic(draft_path, draft.model_dump(mode="json"))
    except Exception as exc:
        print(f"rules extract 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()

    # 规则库记录“哪版提示词、哪个模型”，便于审计同一需求的抽取口径。
    prompt_info = rule_prompt_metadata(context, llm)

    try:
        previous = load_rule_set(rule_base) if rule_base.is_file() else None
        result = import_rule_draft(
            project=project.name,
            context=context,
            draft=draft,
            previous=previous,
            prompt=prompt_info,
            dry_run=args.dry_run,
            path=rule_base,
        )
    except Exception as exc:
        print(f"rules extract 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = {
        "context_id": prepared.context_id,
        "context_dir": str(prepared.context_dir),
        "context": str(prepared.context_path),
        "prompt": str(prepared.prompt_path),
        "schema": str(prepared.schema_path),
        "draft": str(draft_path),
        "rule_base": str(rule_base),
        "written": result.written,
        "version": result.ruleset.version,
        "business_rules": len(result.ruleset.rules),
        "contract_rules": len(result.ruleset.contract_rules),
        "questions": len(result.ruleset.questions),
        "gaps": len(result.ruleset.gaps),
        "conflicts": [conflict.topic for conflict in result.ruleset.conflicts],
        "validation": result.validation.as_dict(),
        "diff": result.diff.as_dict(),
        "warnings": [
            *(context.get("warnings") or []),
            *result.validation.warnings,
        ],
    }
    if result.written:
        # 人工评审文档只读：用于和产品方对齐需求缺陷，不参与导入。
        review_path = rule_base.with_suffix(".md")
        review_path.write_text(
            render_rule_set_markdown(result.ruleset), encoding="utf-8"
        )
        payload["review"] = str(review_path)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if result.validation.errors:
        return 1
    return 0


def _rules_prepare(args) -> int:
    project = Path(args.project).resolve()
    try:
        prepared = prepare_rules_context(
            project,
            _rule_source_documents(args),
            openapi=args.openapi,
            out=args.out,
            prompt_path=args.prompt,
        )
    except Exception as exc:
        print(f"rules prepare 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = {
        "context_id": prepared.context_id,
        "context_dir": str(prepared.context_dir),
        "context": str(prepared.context_path),
        "prompt": str(prepared.prompt_path),
        "schema": str(prepared.schema_path),
        "documents": len(prepared.context.get("documents") or []),
        "operations": len(prepared.context.get("operations") or []),
        "warnings": prepared.context.get("warnings") or [],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _rules_import(args) -> int:
    from .agent.rule_analysis import rule_prompt_metadata

    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        context = load_rules_context(args.context)
        draft = load_rule_draft(args.draft)
        previous = load_rule_set(rule_base) if rule_base.is_file() else None
        result = import_rule_draft(
            project=project.name,
            context=context,
            draft=draft,
            previous=previous,
            prompt=rule_prompt_metadata(context),
            dry_run=args.dry_run,
            path=rule_base,
        )
    except (RuleStoreError, Exception) as exc:
        print(f"rules import 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = result.as_dict()
    payload["rule_base"] = str(rule_base)
    if result.written:
        # 人工评审文档是本次抽取的只读视图：随规则库一起产出，
        # 用来和产品方对齐需求缺陷；它不参与导入，也不能反向修改规则。
        review_path = rule_base.with_suffix(".md")
        review_path.write_text(
            render_rule_set_markdown(result.ruleset), encoding="utf-8"
        )
        payload["review"] = str(review_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if result.validation.errors:
        return 1
    return 0


def _rules_status(args) -> int:
    from .cases.store import approved_cases

    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        ruleset = load_rule_set(rule_base)
    except Exception as exc:
        print(f"rules status 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    status_counts: dict[str, int] = {}
    authority_counts: dict[str, int] = {}
    for rule in ruleset.all_rules():
        status_counts[rule.status.value] = status_counts.get(rule.status.value, 0) + 1
        authority_counts[rule.authority.value] = authority_counts.get(rule.authority.value, 0) + 1
    case_base = _resolve_under_project(project, args.case_base) or project / "caseset.json"
    caseset = load_case_set(case_base) if case_base.is_file() else None
    coverage = None
    orphaned: dict[str, list[str]] = {}
    if caseset is not None:
        cases = approved_cases(caseset)
        coverage = rule_coverage(ruleset, cases).as_dict()
        known = ruleset.rule_index()
        for entry in cases:
            for ref in entry.rule_refs:
                if ref.rule_key not in known:
                    orphaned.setdefault(ref.rule_key, []).append(entry.id)
    payload = {
        "rule_base": str(rule_base),
        "ruleset_id": ruleset.ruleset_id,
        "version": ruleset.version,
        "hash": rule_set_hash(ruleset),
        "business_rules": len(ruleset.rules),
        "contract_rules": len(ruleset.contract_rules),
        "status": status_counts,
        "authority": authority_counts,
        "questions": [item.model_dump(mode="json") for item in ruleset.questions],
        "gaps": [item.model_dump(mode="json") for item in ruleset.gaps],
        "conflicts": [
            item.model_dump(mode="json") for item in ruleset.conflicts
        ],
        "conflict_count": len(ruleset.conflicts),
        "rule_drift_stale_cases": orphaned,
        "case_base": str(case_base) if caseset is not None else None,
        "coverage": coverage,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _rules_render(args) -> int:
    project = Path(args.project).resolve()
    rule_base = _resolve_under_project(project, args.rule_base) or project / "ruleset.json"
    try:
        ruleset = load_rule_set(rule_base)
    except Exception as exc:
        print(f"rules render 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    markdown = render_rule_set_markdown(ruleset)
    if args.stdout:
        print(markdown)
        return 0

    out = _resolve_under_project(project, args.out) or rule_base.with_suffix(".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    payload = {
        "rule_base": str(rule_base),
        "out": str(out),
        "version": ruleset.version,
        "rules": len(ruleset.rules),
        "conflicts": len(ruleset.conflicts),
        "gaps": len(ruleset.gaps),
        "questions": len(ruleset.questions),
        "bytes": len(markdown.encode("utf-8")),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"已渲染 {out}")
    return 0


def _data_path(project: Path, raw: str | None, default_name: str) -> Path:
    path = Path(raw) if raw else project / ".astsight" / "data" / default_name
    if not path.is_absolute():
        path = project / path
    if path.suffix.lower() != ".json":
        path = path / default_name
    return path.resolve()


def _data_catalog(args) -> int:
    project = Path(args.project).resolve()
    try:
        text = "\n;\n".join(
            Path(_resolve_under_project(project, raw) or raw).read_text(
                encoding="utf-8-sig"
            )
            for raw in args.ddl
        )
        parsed = parse_ddl(text, dialect=args.dialect)
        seed = load_seed(_resolve_under_project(project, args.seed), parsed)
        context_id = None
        sources: dict[str, Any] = {}
        if args.context:
            context = load_rules_context(args.context)
            context_id = str(context.get("context_id") or "")
        catalog = build_catalog(
            project=project.name,
            ddl=parsed,
            seed=seed,
            context_id=context_id,
            dialect=args.dialect,
            sources=sources,
        )
        errors = validate_catalog(catalog)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        out = _data_path(project, args.out, "data_catalog.json")
        save_json_atomic(out, catalog.model_dump(mode="json"))
    except Exception as exc:
        print(f"data catalog 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "catalog": str(out),
                "catalog_id": catalog.catalog_id,
                "tables": len(catalog.tables),
                "foreign_keys": len(catalog.foreign_keys),
                "tables_order": catalog.tables_order,
                "gaps": len(catalog.gaps),
                "warnings": catalog.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _data_semantics(args) -> int:
    project = Path(args.project).resolve()
    if args.draft and args.llm:
        print("--draft 与 --llm 只能二选一", file=sys.stderr)
        return 1
    if not args.draft and not args.llm:
        print(
            "需要 --draft（框架外模型输出）或 --llm（框架内调用模型）",
            file=sys.stderr,
        )
        return 1
    try:
        catalog = DataCatalog.model_validate(load_json(args.catalog))
        ruleset = _load_optional_ruleset(project, args.ruleset)
        if args.llm:
            from .agent.data_prep import generate_data_semantics
            from .llm import LLMClient, load_config

            context = None
            if args.context:
                context_path = _resolve_under_project(project, args.context)
                context = load_json(context_path)
            _draft, result = generate_data_semantics(
                catalog,
                llm=LLMClient(load_config(config_path=args.llm_config)),
                ruleset=ruleset,
                context=context if isinstance(context, dict) else None,
                prompt_path=args.prompt,
            )
            catalog = result.catalog
        else:
            draft_path = _resolve_under_project(project, args.draft)
            draft = SemanticDraft.model_validate(load_json(draft_path))
            result = apply_semantics(catalog, draft, ruleset=ruleset)
        if not result.ok:
            print(
                json.dumps(
                    {"ok": False, "errors": result.errors, "warnings": result.warnings},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        out = _data_path(project, args.out, "data_catalog.json")
        save_json_atomic(out, catalog.model_dump(mode="json"))
    except Exception as exc:
        print(f"data semantics 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "catalog": str(out),
                "fields": result.applied_fields,
                "states": result.applied_states,
                "warnings": result.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _data_plan(args) -> int:
    project = Path(args.project).resolve()
    try:
        catalog = DataCatalog.model_validate(load_json(args.catalog))
        errors = validate_catalog(catalog)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        case_base = _resolve_under_project(project, args.cases) or project / "caseset.json"
        caseset = load_case_set(case_base)
        if caseset is None:
            print(f"用例库不存在: {case_base}", file=sys.stderr)
            return 1
        from .cases.store import approved_cases

        ruleset = _load_optional_ruleset(project, args.ruleset)
        payloads: dict[str, Any] = {}
        if args.fixtures:
            raw = load_json(args.fixtures)
            if isinstance(raw, dict) and "plans" in raw:
                payloads = dict(raw.get("plans") or {})
            elif isinstance(raw, dict):
                payloads = dict(raw)
        bundle = build_data_plan_bundle(
            cases=approved_cases(caseset),
            catalog=catalog,
            ruleset=ruleset,
            payloads=payloads,
            run_scope=args.run_scope or "",
        )
        out = _data_path(project, args.out, "data_plan.json")
        save_json_atomic(out, bundle.model_dump(mode="json"))
    except Exception as exc:
        print(f"data plan 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": not bundle.rejected,
                "plan": str(out),
                "run_scope": bundle.run_scope,
                "plans": len(bundle.plans),
                "rejected": [
                    item.model_dump(mode="json") for item in bundle.rejected
                ],
                "warnings": bundle.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if bundle.rejected else 0


def _data_apply(args) -> int:
    try:
        data = load_json(args.plan)
        if isinstance(data, dict) and "plans" in data:
            bundle = DataPlanBundle.model_validate(data)
        else:
            bundle = DataPlanBundle.model_validate(
                {"schema_version": 1, "run_scope": "", "plans": [data]}
            )
        result = apply_bundle(
            db_url=args.db_url,
            bundle=bundle,
            run_scope=args.run_scope,
            keep=args.keep_data,
        )
    except Exception as exc:
        print(f"data apply 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


def _sut_id(raw: str) -> str:
    value = "".join("-" if ch.isspace() else ch for ch in str(raw or "")).strip("-")
    return value or "sut"


def _mock_plan_command(args) -> int:
    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"项目目录不存在: {project}", file=sys.stderr)
        return 1

    notes: list[str] = []
    openapi_source = discover_openapi(project, args.openapi, None)
    operations = []
    if openapi_source:
        try:
            operations = parse_operations(load_spec(openapi_source))
        except Exception as exc:
            notes.append(
                f"openapi_unreadable:{type(exc).__name__}:{str(exc)[:200]}"
            )
    else:
        notes.append("openapi_missing:profile_contract_incomplete")

    try:
        routes, _audit, client, graph_notes = inventory_routes(
            project,
            codegraph=args.codegraph,
            codegraph_module_root=args.codegraph_module_root,
            full_index=True if args.full_index else None,
            openapi_source=openapi_source,
        )
    except Exception as exc:
        print(f"路由清单失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not routes:
        print("未发现任何路由，无法生成 Mock 计划（不猜测）", file=sys.stderr)
        return 1

    profile_routes = [
        {
            "method": route.get("method", ""),
            "path": route.get("path", ""),
            "handler": route.get("handler", ""),
            "owner": route.get("handler_owner", ""),
            "file": route.get("file", ""),
            "source": route.get("source", ""),
            "confidence": route.get("confidence", "inferred"),
        }
        for route in routes
    ]
    try:
        profiles, _unmapped = build_profiles(
            profile_routes,
            operations,
            codegraph_client=client,
            project_root=project,
        )
    except Exception as exc:
        print(f"下游识别失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = build_plan_from_profiles(
        profiles,
        sut_id=_sut_id(args.sut_id or project.name),
        host=args.host,
        port=args.port,
    )
    if not payload["routes"]:
        print(
            "未识别到任何可静态确定的 HTTP 下游调用点；不生成空计划。"
            "请检查 handler 是否可解析、是否使用了未支持的客户端写法。",
            file=sys.stderr,
        )
        return 1

    try:
        model = MockPlan.model_validate(payload)
        validation = validate_mock_plan(model)
    except (MockPlanError, ValueError) as exc:
        print(f"生成的计划未通过校验: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else Path(".astsight/mock-plan.json")
    if not out_path.is_absolute():
        out_path = project / out_path
    out_path = out_path.resolve()
    if out_path.exists() and not args.force:
        print(
            f"输出已存在，拒绝覆盖（确认可覆盖后加 --force）: {out_path}",
            file=sys.stderr,
        )
        return 1
    try:
        save_json_atomic(out_path, model.model_dump(mode="json"))
    except OSError as exc:
        print(f"写入计划失败: {exc}", file=sys.stderr)
        return 1

    warnings: list[str] = [*notes, *graph_notes, *validation.warnings]
    unsupported_calls: dict[str, dict] = {}
    call_site_count = 0
    for profile in profiles:
        warnings.extend(profile.get("downstream_warnings") or [])
        call_site_count += len(profile.get("downstream_call_sites") or [])
        for item in profile.get("downstream_unsupported") or []:
            unsupported_calls.setdefault(item.get("call_site_id", ""), item)
    warnings = list(dict.fromkeys(item for item in warnings if item))

    unsupported_injections = sum(
        1 for item in model.injections if item.strategy.value == "unsupported"
    )
    result = {
        "ok": True,
        "plan": str(out_path),
        "sut_id": model.sut_id,
        "routes": len(model.routes),
        "injections": len(model.injections),
        "unsupported_injections": unsupported_injections,
        "downstream_call_sites": call_site_count,
        "downstream_unsupported": list(unsupported_calls.values()),
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Mock 计划草案: {result['plan']}")
        print(
            f"  sut_id: {result['sut_id']}  路由: {result['routes']}  "
            f"注入: {result['injections']}（unsupported {unsupported_injections}）"
        )
        print(
            f"  下游调用点: {call_site_count}  "
            f"未识别: {len(unsupported_calls)}"
        )
        for warning in warnings:
            print(f"  [WARN] {warning}")
        print(
            "  注意：分支留空，注入原值未知的项标 unsupported；"
            "请人工补全 branches / original 后再用 --mock-plan 执行。"
        )
    return 0


def _mock_semantics_command(args) -> int:
    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"项目目录不存在: {project}", file=sys.stderr)
        return 1
    if args.draft and args.llm:
        print("--draft 与 --llm 只能二选一", file=sys.stderr)
        return 1

    warnings: list[str] = []
    try:
        context_path = _resolve_under_project(project, args.context)
        context = load_context(context_path)
        ruleset = _load_optional_ruleset(project, args.ruleset)
        semantic = build_mock_semantic(context, ruleset)

        if args.draft:
            draft_path = _resolve_under_project(project, args.draft)
            applied = apply_mock_semantic_candidates(
                semantic,
                load_json(draft_path),
                context=context,
                ruleset=ruleset,
            )
            warnings.extend(applied.warnings)
            if not applied.ok:
                print(
                    json.dumps(
                        {"ok": False, "errors": applied.errors, "warnings": applied.warnings},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 1
            semantic = applied.semantic

        if args.llm:
            from .agent.mock_semantics import generate_mock_semantic_candidates
            from .llm import LLMClient, load_config

            applied = generate_mock_semantic_candidates(
                context,
                semantic,
                ruleset=ruleset,
                llm=LLMClient(load_config(config_path=args.llm_config)),
                prompt_path=args.prompt,
            )
            if applied is not None:
                warnings.extend(applied.warnings)
                if not applied.ok:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "errors": applied.errors,
                                "warnings": applied.warnings,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 1
                semantic = applied.semantic

        errors = validate_mock_semantic(semantic, context, ruleset=ruleset)
        if errors:
            print(
                json.dumps(
                    {"ok": False, "errors": errors, "warnings": warnings},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

        out = (
            _resolve_under_project(project, args.out)
            if args.out
            else project / ".astsight" / "mock_semantics.json"
        )
        save_json_atomic(out, semantic.model_dump(mode="json"))
    except Exception as exc:
        print(f"mock semantics 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    result = {
        "ok": True,
        "semantics": str(out),
        "context_id": semantic.context_id,
        "deps": len(semantic.deps),
        "branches": sum(len(dep.branches) for dep in semantic.deps),
        "gaps": len(semantic.gaps),
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Mock 语义: {result['semantics']}")
        print(
            f"  deps: {result['deps']}  分支: {result['branches']}  "
            f"gaps: {result['gaps']}"
        )
        for warning in warnings:
            print(f"  [WARN] {warning}")
        for gap in semantic.gaps:
            print(f"  [GAP] {gap.dep_id or 'global'}: {gap.reason}")
    return 0


def _split_command(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    tokens = [token.strip('"').strip("'") for token in shlex.split(raw, posix=False)]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise ValueError("--sut-command 解析后为空")
    return tokens


def _parse_sut_env(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--sut-env 需要 KEY=VALUE，实际为 {item!r}")
        env[key.strip()] = value
    return env


def _agent_command(args) -> int:
    from .agent import AgentError, FlowConfig, run_flow

    try:
        config = FlowConfig(
            project=str(Path(args.project).resolve()),
            requirements=list(args.requirements),
            openapi=args.openapi,
            llm_config=args.llm_config,
            mode=args.mode,
            base=args.base,
            target=args.target,
            depth=args.depth,
            base_url=args.base_url,
            reset_url=args.reset_url,
            codegraph=args.codegraph,
            codegraph_module_root=args.codegraph_module_root,
            full_index=True if args.full_index else None,
            case_base=args.case_base,
            out=args.out,
            approve_as=args.approve_as,
            max_repair_rounds=max(0, min(5, args.max_repair_rounds)),
            max_cases=max(1, min(500, args.max_cases)),
            sut_command=_split_command(args.sut_command),
            sut_ready_url=args.sut_ready_url,
            sut_env=_parse_sut_env(args.sut_env),
            sut_timeout=args.sut_timeout,
            timeout=args.timeout,
            retries=max(0, min(3, args.retries)),
            collect_only=not args.no_collect_check,
            skip_execute=args.skip_execute,
            allow_unmapped=not args.strict_unmapped,
            min_pass_rate=args.min_pass_rate,
            mock_host=args.mock_host,
            mock_port=args.mock_port,
            rule_base=args.rule_base,
            data_ddl=list(args.ddl),
            data_seed=args.seed,
            data_catalog=args.data_catalog,
            data_plan=args.data_plan,
            database_url=args.db_url,
            data_dialect=args.db_dialect,
            skip_data=args.skip_data,
        )
    except ValueError as exc:
        print(f"agent 参数错误: {exc}", file=sys.stderr)
        return 1

    try:
        result = run_flow(config)
    except AgentError as exc:
        print(f"agent 流程中止: {exc.code}: {exc.detail}", file=sys.stderr)
        details = exc.details
        if details:
            print(json.dumps(details, ensure_ascii=False, indent=2)[:8_000], file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"agent 流程中止: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"AstSight agent: {result.run_id}")
        print(f"  状态: {result.status}")
        for name in ("scope", "rules", "mock_semantics", "data_catalog",
                     "case_design", "proposal", "approved_cases", "data_plan",
                     "data_apply", "mock", "sut", "execution", "diagnosis"):
            if name in result.steps:
                value = result.steps[name]
                if isinstance(value, (list, tuple)):
                    print(f"  {name}: {len(value)}")
                else:
                    print(f"  {name}: {value}")
        for warning in result.warnings:
            print(f"  [WARN] {warning}")
        for error in result.errors:
            print(f"  [ERROR] {error}")
        print(f"  产物: {result.run_dir}")
        if result.report is not None:
            artifacts = result.report.get("artifacts") or {}
            if artifacts.get("report_md"):
                print(f"  报告: {artifacts['report_md']}")
        if result.llm_usage:
            print(f"  模型调用: {len(result.llm_usage)} 次")

    if result.status == "pass":
        return 0
    if result.status in ("fail", "pending_review", "not_run"):
        return 2 if result.status in ("pending_review", "not_run") else 1
    return 1


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
        if args.cases_command == "review":
            return _cases_review(args)
        if args.cases_command == "status":
            return _cases_status(args)
    if args.command == "mock":
        if args.mock_command == "plan":
            return _mock_plan_command(args)
        if args.mock_command == "semantics":
            return _mock_semantics_command(args)
    if args.command == "rules":
        if args.rules_command in ("extract", "c1"):
            return _rules_extract(args)
        if args.rules_command == "prepare":
            return _rules_prepare(args)
        if args.rules_command == "import":
            return _rules_import(args)
        if args.rules_command == "status":
            return _rules_status(args)
        if args.rules_command == "render":
            return _rules_render(args)
    if args.command == "data":
        if args.data_command == "catalog":
            return _data_catalog(args)
        if args.data_command == "semantics":
            return _data_semantics(args)
        if args.data_command == "plan":
            return _data_plan(args)
        if args.data_command == "apply":
            return _data_apply(args)
    if args.command == "codegraph":
        if args.codegraph_command == "install":
            return _codegraph_install(args)
        if args.codegraph_command == "status":
            return _codegraph_status(args)
    if args.command == "agent":
        if args.agent_command == "run":
            return _agent_command(args)
    parser.print_help()
    return 2


def _codegraph_install(args) -> int:
    try:
        result = install_codegraph_runtime(
            args.root, version=args.version, force=args.force
        )
    except Exception as exc:  # 环境问题：npm/node/网络，原样报出
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        action = "已复用" if result.get("reused") else "已安装"
        print(f"CodeGraph {result.get('version')} {action}: {result['root']}")
        print("后续运行默认使用该目录；--codegraph-module-root 可显式覆盖。")
    return 0


def _codegraph_status(args) -> int:
    data = codegraph_runtime_status(args.root)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"CodeGraph 包: {data['package']} (pin {data['pinned_version']})")
        print(f"持久化目录: {data['managed_root']}")
        print(f"实际使用: {data['module_root'] or '未找到（运行时会回退 bridge 目录）'}")
        print(
            f"安装状态: {'已安装 ' + str(data['installed_version']) if data['installed'] else '未安装'}"
        )
        print(f"Node: {data['node'] or '未找到'} / npm: {data['npm'] or '未找到'}")
        if not data["installed"]:
            print("安装命令: python -m astsight codegraph install")
    return 0 if data["installed"] else 1


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
