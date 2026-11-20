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
    parse_dbml,
    parse_ddl,
    reflect_database,
    validate_catalog,
    validate_plan_fixtures,
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
from .feedback import collect_feedback, open_issue
from .commands._shared import (
    _load_optional_catalog,
    _load_optional_mock_semantic,
    _load_optional_ruleset,
    _resolve_under_project,
)
from .commands.data import (
    _data_apply, _data_catalog, _data_plan, _data_semantics, _data_states,
)
from .commands.init import initialize as initialize_llm_config
from .commands.mock import _mock_plan_command, _mock_semantics_command
from .commands.rules import (
    _rules_extract, _rules_import, _rules_prepare, _rules_render, _rules_status,
)
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
    run_parser.add_argument(
        "--dubbo-url", default=None,
        help="被测 Dubbo 服务直连地址（dubbo:// 或 protobuf unary 的 tri://host:port）",
    )
    run_parser.add_argument(
        "--dubbo-host", default=None,
        help="Dubbo 目标主机；画像只解析出 hessian2 端口时拼 dubbo://host:port",
    )
    run_parser.add_argument(
        "--dubbo-target", default=None,
        help="SUT 直连地址 IP:port（或 dubbo://IP:port / tri://IP:port）；只补未解析服务",
    )
    run_parser.add_argument(
        "--dubbo-targets", default=None,
        help="服务级直连目标映射 JSON；键为 interface?group=&version=",
    )
    run_parser.add_argument(
        "--dubbo-source-sha256", default=None,
        help="期望的 SUT 构建产物 sha256；需配合 --dubbo-artifact",
    )
    run_parser.add_argument(
        "--dubbo-source-commit", default=None,
        help="期望的 SUT git commit；与画像来源不一致时阻断",
    )
    run_parser.add_argument(
        "--dubbo-source-branch", default=None,
        help="期望的 SUT git branch",
    )
    run_parser.add_argument(
        "--dubbo-artifact", default=None,
        help="用于校验 sha256 的 SUT 构建产物路径",
    )
    run_parser.add_argument(
        "--dubbo-target-override", action="store_true",
        help="允许服务级映射覆盖代码里的显式 Dubbo 地址",
    )
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
        "--gripmock-path",
        default=None,
        help="gRPC Mock Plan 使用的 gripmock 可执行文件",
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
    run_parser.add_argument(
        "--feedback",
        choices=("auto", "always", "off"),
        default="auto",
        help="诊断包生成策略：auto=门禁失败时生成，always=总是生成，off=关闭",
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
        help="C1：需求+设计（可选）+ContractModel → ruleset.json + ruleset.md（调用模型）",
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
        help="可选：OpenAPI 契约源；不传时自动发现，并继续扫描 Spring MVC、gRPC descriptor 与 Dubbo 接口",
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

    data_catalog = data_sub.add_parser(
        "catalog", help="DDL/DBML/数据库反射 → data_catalog.json"
    )
    data_catalog.add_argument("--project", required=True)
    source_group = data_catalog.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--ddl", action="append", metavar="PATH", help="DDL 文件（可重复）"
    )
    source_group.add_argument("--dbml", default=None, metavar="PATH", help="DBML 文件")
    source_group.add_argument(
        "--db-url", default=None, help="从真实数据库反射结构（sqlite/postgres/mysql）"
    )
    data_catalog.add_argument("--seed", default=None)
    data_catalog.add_argument(
        "--schema",
        default=None,
        help="反射的 schema/database；多个用逗号分隔（仅 --db-url）",
    )
    data_catalog.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="只反射匹配的表名（仅 --db-url，可重复）",
    )
    data_catalog.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="跳过匹配的表名（仅 --db-url，可重复）",
    )
    data_catalog.add_argument(
        "--dialect",
        default=None,
        choices=("sqlite", "postgres", "mysql"),
        help="造数 SQL 方言；--db-url 时从 URL 推断，其余默认 sqlite",
    )
    data_catalog.add_argument("--context", default=None, help="rules context 目录/JSON")
    data_catalog.add_argument(
        "--ruleset",
        default=None,
        help="规则库 JSON；eq/in 常量会折叠为字段取值候选（确定性，不调用模型）",
    )
    data_catalog.add_argument(
        "--code",
        action="append",
        default=[],
        metavar="PATH",
        help="扫描 Java/Python/Go/TS 枚举与常量组的目录或文件（可重复）",
    )
    data_catalog.add_argument(
        "--rule-states",
        action="store_true",
        help="按规则跨表约束生成候选 DataState 骨架（需要 --ruleset）",
    )
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

    data_states = data_sub.add_parser(
        "states", help="查看数据目录中的业务态候选、target 与占位 gap"
    )
    data_states.add_argument("--project", required=True)
    data_states.add_argument("--catalog", required=True)
    data_states.add_argument("--state", default=None, help="只看指定 state_key")
    data_states.add_argument("--json", action="store_true")

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
    agent_run.add_argument(
        "--dubbo-url", default=None,
        help="被测 Dubbo 服务直连地址（dubbo:// 或 protobuf unary 的 tri://host:port）",
    )
    agent_run.add_argument(
        "--dubbo-host", default=None,
        help="Dubbo 目标主机；画像只解析出 hessian2 端口时拼 dubbo://host:port",
    )
    agent_run.add_argument(
        "--dubbo-target", default=None,
        help="SUT 直连地址 IP:port（或 dubbo://IP:port / tri://IP:port）；只补未解析服务",
    )
    agent_run.add_argument(
        "--dubbo-targets", default=None,
        help="服务级直连目标映射 JSON；键为 interface?group=&version=",
    )
    agent_run.add_argument(
        "--dubbo-source-sha256", default=None,
        help="期望的 SUT 构建产物 sha256；需配合 --dubbo-artifact",
    )
    agent_run.add_argument(
        "--dubbo-source-commit", default=None,
        help="期望的 SUT git commit；与画像来源不一致时阻断",
    )
    agent_run.add_argument(
        "--dubbo-source-branch", default=None,
        help="期望的 SUT git branch",
    )
    agent_run.add_argument(
        "--dubbo-artifact", default=None,
        help="用于校验 sha256 的 SUT 构建产物路径",
    )
    agent_run.add_argument(
        "--dubbo-target-override", action="store_true",
        help="允许服务级映射覆盖代码里的显式 Dubbo 地址",
    )
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
    agent_run.add_argument(
        "--gripmock-path",
        default=None,
        help="gRPC 下游 Mock 使用的 gripmock 可执行文件",
    )
    agent_run.add_argument(
        "--feedback",
        choices=("auto", "always", "off"),
        default="auto",
        help="诊断包生成策略：auto=门禁失败时生成，always=总是生成，off=关闭",
    )
    agent_run.add_argument("--json", action="store_true")

    init_parser = sub.add_parser(
        "init", help="生成默认模型配置（默认 ~/.astsight/llm.json）"
    )
    init_parser.add_argument(
        "--provider", choices=("anthropic", "openai"), default=None
    )
    init_parser.add_argument("--model", default=None)
    init_parser.add_argument("--base-url", default=None)
    init_parser.add_argument(
        "--auth-style", choices=("x-api-key", "bearer"), default=None
    )
    init_parser.add_argument("--api-key-env", default=None)
    init_parser.add_argument(
        "--local", action="store_true", help="写入当前目录的 llm.json"
    )
    init_parser.add_argument("--out", default=None)
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument(
        "--yes", action="store_true", help="不询问，直接使用检测到的默认值"
    )
    init_parser.add_argument("--json", action="store_true")

    feedback_parser = sub.add_parser(
        "feedback", help="本地诊断包与 GitHub Issue 反馈（默认脱敏、不自动上传）"
    )
    feedback_sub = feedback_parser.add_subparsers(
        dest="feedback_command", required=True
    )
    feedback_collect = feedback_sub.add_parser(
        "collect", help="从 run 目录生成脱敏诊断包 zip"
    )
    feedback_collect.add_argument("--run", required=True, help="run 目录")
    feedback_collect.add_argument(
        "--out", default=None, help="输出 zip 路径；默认 <run>/feedback.zip"
    )
    feedback_collect.add_argument(
        "--no-case-code", action="store_true", help="不包含失败用例代码"
    )
    feedback_collect.add_argument(
        "--max-bytes", type=int, default=10 * 1024 * 1024
    )
    feedback_collect.add_argument(
        "--dry-run", action="store_true", help="只列出将包含的文件，不写 zip"
    )
    feedback_collect.add_argument("--json", action="store_true")

    feedback_open = feedback_sub.add_parser(
        "open", help="生成 issue.md 与 GitHub new-issue 链接"
    )
    feedback_open.add_argument("--run", required=True, help="run 目录")
    feedback_open.add_argument(
        "--repo", default=None, help="owner/repo；默认读 ASTSIGHT_REPO 或 git remote"
    )
    feedback_open.add_argument(
        "--out", default=None, help="issue.md 路径；默认 <run>/issue.md"
    )
    feedback_open.add_argument(
        "--bundle", default="feedback.zip", help="issue 中引用的附件名"
    )
    feedback_open.add_argument("--json", action="store_true")

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
        dubbo_url=args.dubbo_url,
        dubbo_host=args.dubbo_host,
        dubbo_target=args.dubbo_target,
        dubbo_targets=args.dubbo_targets,
        dubbo_source_sha256=args.dubbo_source_sha256,
        dubbo_source_commit=args.dubbo_source_commit,
        dubbo_source_branch=args.dubbo_source_branch,
        dubbo_artifact=args.dubbo_artifact,
        dubbo_target_override=args.dubbo_target_override,
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
        gripmock_path=args.gripmock_path,
        mock_admin_url=args.mock_admin_url,
        mock_token=args.mock_token,
        mock_namespace=args.mock_namespace,
        mock_run_scope=args.mock_run_scope,
        mock_drain_ms=args.mock_drain_ms,
        mock_ttl_ms=args.mock_ttl_ms,
        mock_evidence=args.mock_evidence,
        feedback=args.feedback,
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
            dubbo_url=args.dubbo_url,
            dubbo_host=args.dubbo_host,
            dubbo_target=args.dubbo_target,
            dubbo_targets=args.dubbo_targets,
            dubbo_source_sha256=args.dubbo_source_sha256,
            dubbo_source_commit=args.dubbo_source_commit,
            dubbo_source_branch=args.dubbo_source_branch,
            dubbo_artifact=args.dubbo_artifact,
            dubbo_target_override=args.dubbo_target_override,
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
            gripmock_path=args.gripmock_path,
            feedback=args.feedback,
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
        if args.data_command == "states":
            return _data_states(args)
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
    if args.command == "init":
        return _init_command(args)
    if args.command == "feedback":
        if args.feedback_command == "collect":
            return _feedback_collect(args)
        if args.feedback_command == "open":
            return _feedback_open(args)
    parser.print_help()
    return 2


def _init_command(args) -> int:
    try:
        result = initialize_llm_config(
            out=Path(args.out) if args.out else None,
            local=args.local,
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            auth_style=args.auth_style,
            api_key_env=args.api_key_env,
            force=args.force,
            interactive=not args.yes,
        )
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"已生成模型配置: {result['path']}")
    print(f"  provider: {result['provider']}")
    print(f"  model: {result['model']}")
    if result.get("base_url"):
        print(f"  base_url: {result['base_url']}")
    print(f"  API key 环境变量: {result['api_key_env']}")
    if result.get("key_present"):
        print("  已从当前环境检测到 key。")
    print("下一步:")
    for step in result.get("next_steps") or []:
        print(f"  {step}")
    return 0


def _feedback_collect(args) -> int:
    try:
        result = collect_feedback(
            args.run,
            args.out,
            include_case_code=not args.no_case_code,
            max_bytes=args.max_bytes,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if result.get("dry_run"):
        print(
            f"诊断包预览（未写文件）：{len(result['files'])} 个文件，"
            f"约 {result['size']} 字节"
        )
        for item in result["files"]:
            print(f"  - {item['name']} ({item['bytes']} bytes)")
    else:
        print(f"已生成诊断包: {result['path']}")
        print(f"文件 {len(result['files'])} 个，约 {result['size']} 字节")
    if result.get("truncated"):
        print("因大小限制未包含: " + ", ".join(result["truncated"]))
    if not result.get("dry_run"):
        print("诊断包已在本地脱敏；提交前请自行检查。")
        print(f"下一步: astsight feedback open --run {args.run}")
    return 0


def _feedback_open(args) -> int:
    try:
        result = open_issue(
            args.run,
            repo=args.repo,
            out=args.out,
            bundle=args.bundle,
        )
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"已生成 issue 草稿: {result['issue_md']}")
    print(f"仓库: {result['repo']}")
    print(f"提交页: {result['url']}")
    if result.get("truncated_url"):
        print("提示: URL 中的正文已截断，完整内容见 issue.md。")
    print(f"请先执行 feedback collect，再把 {result['bundle']} 拖入 issue。")
    return 0


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
        if result.get("source") == "packaged":
            print(
                f"CodeGraph {result.get('version')} 已随平台 wheel 提供: "
                f"{result['root']}"
            )
            print("Node 与 CodeGraph 均来自 astsight-codegraph-bin，无需另装。")
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
        print(f"来源: {data.get('source') or '未安装'}")
        print(f"持久化目录: {data['managed_root']}")
        print(f"实际使用: {data['module_root'] or '未找到（运行时会回退 bridge 目录）'}")
        print(
            f"安装状态: {'已安装 ' + str(data['installed_version']) if data['installed'] else '未安装'}"
        )
        print(f"Node: {data['node'] or '未找到'} / npm: {data['npm'] or '未找到'}")
        if data.get("source") == "packaged":
            print("Node 与 CodeGraph 由 astsight-codegraph-bin 平台 wheel 提供，无需安装。")
        elif not data["installed"]:
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
