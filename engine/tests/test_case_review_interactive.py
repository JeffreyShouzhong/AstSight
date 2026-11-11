"""交互式审核：只写决定，不自动批准。"""
from __future__ import annotations

import json
from pathlib import Path

from astsight.cases.proposal import interactive_review


def _candidate(case_key: str, *, steps: bool = False) -> dict:
    candidate = {
        "endpoint": "POST /orders",
        "rule_refs": [
            {"rule_key": "RK-0123456789ab", "rule_id": "BR-01"}
        ],
        "requirement_refs": [
            {"doc": "docs/api.md", "section": "订单服务 > POST /orders"}
        ],
        "preconditions": [],
        "steps": [],
        "asserts": [{"kind": "status_code", "op": "eq", "expect": 201}],
        "rationale": "规则 BR-01 给出成功状态码",
        "warnings": [],
    }
    if steps:
        candidate["steps"] = [
            {
                "name": "create",
                "endpoint": "POST /orders",
                "body": {"quantity": 1},
                "asserts": [
                    {"kind": "status_code", "op": "eq", "expect": 201}
                ],
            },
            {
                "name": "pay",
                "endpoint": "PATCH /orders/{order_id}",
                "path_params": {"order_id": {"$ref": "create.order_id"}},
                "asserts": [
                    {"kind": "status_code", "op": "eq", "expect": 200}
                ],
            },
        ]
    return candidate


def _proposal(tmp_path: Path, decisions: list[str]) -> tuple[Path, list[str]]:
    items = []
    review_items = []
    keys = []
    for index, decision in enumerate(decisions, 1):
        case_key = f"case_{index}"
        proposal_key = f"POST /orders::{case_key}"
        keys.append(proposal_key)
        items.append(
            {
                "proposal_key": proposal_key,
                "action": "new",
                "case_key": case_key,
                "endpoint": "POST /orders",
                "title": f"用例 {index}",
                "fingerprint": "0" * 64,
                "store_case_id": None,
                "current_version": None,
                "proposed_version": 1,
                "candidate": _candidate(case_key, steps=True),
                "existing": {},
                "warnings": [],
            }
        )
        review_items.append(
            {
                "proposal_key": proposal_key,
                "action": "new",
                "case_key": case_key,
                "endpoint": "POST /orders",
                "store_case_id": None,
                "fingerprint": "0" * 64,
                "title": f"用例 {index}",
                "decision": decision,
                "reviewer": None,
                "note": None,
            }
        )
    proposal_dir = tmp_path / "prop-test"
    proposal_dir.mkdir()
    (proposal_dir / "proposal.json").write_text(
        json.dumps(
            {
                "proposal_id": "prop-test",
                "base_ruleset": {},
                "items": items,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (proposal_dir / "cases.review.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "proposal_id": "prop-test",
                "items": review_items,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return proposal_dir, keys


def _reader(answers: list[str]):
    queue = iter(answers)
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return next(queue)

    return read, prompts


def _decisions(proposal_dir: Path) -> dict[str, dict]:
    data = json.loads(
        (proposal_dir / "cases.review.json").read_text(encoding="utf-8")
    )
    return {item["case_key"]: item for item in data["items"]}


def test_review_only_pending_and_records_note(tmp_path: Path) -> None:
    proposal_dir, _ = _proposal(tmp_path, ["approve", "pending"])
    read, prompts = _reader(["a 按设计裁决"])
    output: list[str] = []
    result = interactive_review(
        proposal_dir,
        only_pending=True,
        input_fn=read,
        output_fn=output.append,
    )
    decisions = _decisions(proposal_dir)
    assert len(prompts) == 1
    assert decisions["case_1"]["decision"] == "approve"
    assert decisions["case_2"]["decision"] == "approve"
    assert decisions["case_2"]["note"] == "按设计裁决"
    assert result["counts"] == {"approve": 2, "reject": 0, "pending": 0}
    assert result["pending"] == []
    assert any("步骤: 2 步" in line for line in output)


def test_reject_and_skip_leave_other_pending(tmp_path: Path) -> None:
    proposal_dir, _ = _proposal(tmp_path, ["pending", "pending"])
    read, _ = _reader(["r 断言来源不符", "s"])
    result = interactive_review(
        proposal_dir, input_fn=read, output_fn=lambda line: None
    )
    decisions = _decisions(proposal_dir)
    assert decisions["case_1"]["decision"] == "reject"
    assert decisions["case_1"]["note"] == "断言来源不符"
    assert decisions["case_2"]["decision"] == "pending"
    assert result["counts"] == {"approve": 0, "reject": 1, "pending": 1}


def test_quit_saves_previous_decisions(tmp_path: Path) -> None:
    proposal_dir, _ = _proposal(tmp_path, ["pending", "pending"])
    read, _ = _reader(["a", "q"])
    result = interactive_review(
        proposal_dir, input_fn=read, output_fn=lambda line: None
    )
    decisions = _decisions(proposal_dir)
    assert decisions["case_1"]["decision"] == "approve"
    assert decisions["case_2"]["decision"] == "pending"
    assert result["counts"]["pending"] == 1


def test_invalid_input_reprompts(tmp_path: Path) -> None:
    proposal_dir, _ = _proposal(tmp_path, ["pending"])
    read, prompts = _reader(["x", "a"])
    interactive_review(
        proposal_dir, input_fn=read, output_fn=lambda line: None
    )
    assert len(prompts) == 2
    assert _decisions(proposal_dir)["case_1"]["decision"] == "approve"
