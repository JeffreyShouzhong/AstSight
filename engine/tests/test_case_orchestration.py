"""编排 A4.5：待审 proposal 阻断，审核通过后才进入 C6。"""
from __future__ import annotations

from orchestration import build_graph
from orchestration.graph import node_a4_review


def _proposal(*decisions: str) -> dict:
    return {
        "proposal_id": "prop-test",
        "items": [
            {
                "proposal_key": f"POST /orders::case_{index}",
                "decision": decision,
            }
            for index, decision in enumerate(decisions, 1)
        ],
    }


def _doc_state(proposal: dict, reviewed_cases: list | None = None, **extra) -> dict:
    state = {
        "doc_changed": True,
        "code_changed": False,
        "case_proposal": proposal,
        "reviewed_cases": reviewed_cases if reviewed_cases is not None else [{"id": "TC-0001"}],
    }
    state.update(extra)
    return state


def test_pending_decision_blocks_before_c6() -> None:
    graph = build_graph()
    out = graph.invoke(_doc_state(_proposal("pending")))
    assert out["blocked"] is True
    assert "未审核" in out["block_reason"]
    assert out.get("gate_conclusion") is None


def test_missing_decision_blocks() -> None:
    out = node_a4_review(_doc_state({"items": [{"proposal_key": "k"}]}))
    assert out["blocked"] is True
    assert "未审核" in out["block_reason"]


def test_all_approved_reaches_gate_and_c6_uses_reviewed_cases() -> None:
    graph = build_graph()
    cases = [{"id": "TC-0001"}, {"id": "TC-0002"}]
    out = graph.invoke(_doc_state(_proposal("approve", "approve"), cases))
    assert out["blocked"] is False
    assert out["block_reason"] is None
    assert out["cases"] == cases
    assert out.get("gate_conclusion") == "pending"


def test_all_rejected_blocks_for_no_executable_case() -> None:
    graph = build_graph()
    out = graph.invoke(_doc_state(_proposal("reject"), reviewed_cases=[]))
    assert out["blocked"] is True
    assert "没有可执行用例" in out["block_reason"]


def test_review_decisions_override_proposal_pending() -> None:
    graph = build_graph()
    state = _doc_state(_proposal("pending"))
    state["review_decisions"] = {"POST /orders::case_1": "approve"}
    out = graph.invoke(state)
    assert out["blocked"] is False
    assert out.get("gate_conclusion") == "pending"


def test_approved_store_without_proposal_can_run() -> None:
    graph = build_graph()
    out = graph.invoke(
        {
            "doc_changed": True,
            "code_changed": False,
            "case_proposal": None,
            "reviewed_cases": [{"id": "TC-0001"}],
        }
    )
    assert out["blocked"] is False
    assert out["cases"] == [{"id": "TC-0001"}]


def test_code_only_path_does_not_require_case_review() -> None:
    graph = build_graph()
    out = graph.invoke(
        {
            "doc_changed": False,
            "code_changed": True,
            "case_proposal": _proposal("pending"),
        }
    )
    assert out.get("blocked") is None
    assert out.get("gate_conclusion") == "pending"
