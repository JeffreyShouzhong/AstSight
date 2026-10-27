"""编排骨架（LangGraph StateGraph）。

    START
     ├─[入口分流] 文档变更 → A1 需求上下文 → A4 用例导入 → A4.5 审核检查
     ├─[入口分流] 代码变更 → S2 变更感知 → S3 画像
     ├─ 汇入 → C5 造数 → C6 编码 → C7 执行 → 门禁 → END

A4.5 不再检查规则确认，而是检查待审 proposal 的审核决定：
所有 actionable 项必须已有 approve/reject，且至少存在一条可执行用例。
审核前草案不会进入用例库，也不会进入 C6。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .state import EngineState

_PENDING_DECISIONS = {"", "pending"}
_VALID_DECISIONS = {"approve", "reject"}


# ───────────────────────── 节点（stub + 确定性实现）─────────────────────────

def node_entry(state: EngineState) -> EngineState:
    """入口分流：判定本次运行由哪些变更源驱动。"""
    state.setdefault("doc_changed", False)
    state.setdefault("code_changed", True)
    return state


def node_a1(state: EngineState) -> EngineState:
    """A1 需求上下文（stub）：需求文档 + OpenAPI → context 包。

    正式实现挂 `cases prepare`；这里只保留确定性状态传递。
    """
    state["case_context"] = state.get("case_context")
    return state


def node_a4(state: EngineState) -> EngineState:
    """A4 用例导入（stub）：draft → proposal，不直接进入用例库。"""
    state["case_proposal"] = state.get("case_proposal")
    return state


def node_a4_review(state: EngineState) -> EngineState:
    """A4.5 审核结果检查（确定性门）。

    阻断条件：
    - proposal 中仍有 pending/未知决定；
    - 审核后没有任何可执行用例。
    """
    proposal = state.get("case_proposal") or {}
    items = proposal.get("items") or []
    decisions = state.get("review_decisions") or {}
    unresolved: list[str] = []
    for item in items:
        key = str(item.get("proposal_key") or item.get("case_key") or "")
        decision = str(decisions.get(key, item.get("decision") or "pending"))
        if decision not in _VALID_DECISIONS:
            unresolved.append(key or "?")

    reviewed_cases = state.get("reviewed_cases") or state.get("cases") or []
    if unresolved:
        state["blocked"] = True
        state["block_reason"] = f"存在未审核/决定非法的用例项: {unresolved}"
    elif not reviewed_cases:
        state["blocked"] = True
        state["block_reason"] = "审核后没有可执行用例（全部 reject 或用例库为空）"
    else:
        state["blocked"] = False
        state["block_reason"] = None
    return state


def node_s2(state: EngineState) -> EngineState:
    """S2 变更感知：代码变更 → 变更接口（经 CodeGraphClient）。"""
    changed_symbols = state.get("_changed_symbols", [])
    client = state.get("codegraph_client")

    changed_apis = state.get("changed_apis", [])
    if client is None:
        state["changed_apis"] = []
        state["_unsupported"] = changed_symbols
        return state

    for sym in changed_symbols:
        result = client.impact(sym)
        for handler in result.affected:
            changed_apis.append(
                {
                    "handler": handler,
                    "confidence": "high",
                    "trace": result.trace,
                    "changed_symbols": [sym],
                }
            )

    state["changed_apis"] = changed_apis
    return state


def node_s3(state: EngineState) -> EngineState:
    """S3 画像构建（stub）：变更接口 → 画像。"""
    client = state.get("codegraph_client")
    changed_apis = state.get("changed_apis", [])
    profiles = state.get("profiles", [])

    if client is None:
        state["profiles"] = profiles
        return state

    for api in changed_apis:
        handler = api.get("handler", "")
        callees = client.callees(handler) if handler else []
        profiles.append(
            {
                "handler": handler,
                "downstream_deps": [c.name for c in callees],
            }
        )

    state["profiles"] = profiles
    return state


def node_c5(state: EngineState) -> EngineState:
    """C5 数据准备（stub）：画像 → 造数计划。"""
    state["data_plan"] = state.get("data_plan")
    return state


def node_c6(state: EngineState) -> EngineState:
    """C6 用例编码（stub）：已审核用例 → pytest 代码。"""
    state["cases"] = state.get("reviewed_cases") or state.get("cases") or []
    return state


def node_c7_execute(state: EngineState) -> EngineState:
    """C7 执行编排（stub）：执行 + 诊断 + 报告。"""
    return state


def node_gate(state: EngineState) -> EngineState:
    """门禁判定（stub）。"""
    state["gate_conclusion"] = state.get("gate_conclusion", "pending")
    return state


# ───────────────────────── 路由 ─────────────────────────

def route_after_review(state: EngineState) -> str:
    """A4.5 之后：未审核阻断；否则按是否有代码变更继续。"""
    if state.get("blocked"):
        return "blocked"
    return "code" if state.get("code_changed") else "cases"


def build_graph():
    """构建编排图。"""
    g = StateGraph(EngineState)

    g.add_node("entry", node_entry)
    g.add_node("a1", node_a1)
    g.add_node("a4", node_a4)
    g.add_node("a4_review", node_a4_review)
    g.add_node("s2", node_s2)
    g.add_node("s3", node_s3)
    g.add_node("c5", node_c5)
    g.add_node("c6", node_c6)
    g.add_node("c7_execute", node_c7_execute)
    g.add_node("gate", node_gate)

    g.add_edge(START, "entry")
    g.add_conditional_edges(
        "entry",
        lambda s: "doc" if s.get("doc_changed") else "code",
        {"doc": "a1", "code": "s2"},
    )

    # 文档链路：上下文 → 用例导入 → 人工审核结果检查
    g.add_edge("a1", "a4")
    g.add_edge("a4", "a4_review")
    g.add_conditional_edges(
        "a4_review",
        route_after_review,
        {"blocked": END, "code": "s2", "cases": "c5"},
    )

    # 代码链路与汇合后的确定性执行骨架
    g.add_edge("s2", "s3")
    g.add_edge("s3", "c5")
    g.add_edge("c5", "c6")
    g.add_edge("c6", "c7_execute")
    g.add_edge("c7_execute", "gate")
    g.add_edge("gate", END)

    return g.compile()
