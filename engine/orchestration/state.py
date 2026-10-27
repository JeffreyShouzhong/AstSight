"""编排层全局状态（对齐第四章流转图 State）。

LangGraph 的 State 是编排层共享内存，贯穿全流程。
"""
from __future__ import annotations

from typing import TypedDict


class EngineState(TypedDict, total=False):
    """引擎全局状态。

    字段与第四章流转图 State 一致：
    changed_apis / profiles / case_* / data_plan / cases / report / 门禁结论
    """

    # ── 入口分流 ──
    doc_changed: bool          # 需求/设计文档是否有变更
    code_changed: bool         # 代码分支是否有变更

    # ── 各环节产物（契约实体） ──
    case_context: dict | None  # A1 产出：cases prepare 的 context
    case_draft: dict | None    # Skill/人工产出的用例草案
    case_proposal: dict | None # A4 产出：待审 proposal（含 items）
    review_decisions: dict     # proposal_key -> approve/reject
    reviewed_cases: list       # A4.5 通过后进入 C6 的用例
    changed_apis: list         # list[ChangedAPI]，S2 产出
    profiles: list             # list[Profile]，S3 产出
    data_plan: dict | None     # DataPlan，C5 产出
    cases: list                # 用例，C6 输入
    report: dict | None        # Report，C7.4 产出

    # ── 人工断点 ──
    blocked: bool              # 是否存在未审核用例导致阻断
    block_reason: str | None   # 阻断原因

    # ── 门禁 ──
    gate_conclusion: str | None  # 门禁结论

    # ── 基础设施注入（运行时由调用方注入，非产物字段） ──
    codegraph_client: object | None  # CodeGraphClient 实例
    _changed_symbols: list          # 本次变更的符号（S2 输入）
    _unsupported: list | None       # 因语言不支持未追溯的符号
    case_store_path: str | None     # 项目级 caseset.json 路径
