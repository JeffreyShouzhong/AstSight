"""关系抽取评估的打分逻辑（不调用模型）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astsight.evaluation import aggregate, load_corpus, score_case, signatures
from contracts import RuleRelation


def _expected(
    ops: list[str],
    sources: list[str] | None = None,
    paths: list[str] | None = None,
) -> dict:
    return {
        "assertion_kind": "body_field",
        "target": "total",
        "op": "eq",
        "ops": ops,
        "sources": sources if sources is not None else ["response"],
        "paths": paths if paths is not None else [],
    }


def test_score_case_matches_pairs_and_ops() -> None:
    result = score_case(
        [_expected(["sum", "pluck"])],
        [
            _expected(["sum", "pluck"]),
            {
                "assertion_kind": "body_field",
                "target": "extra",
                "op": "eq",
                "ops": ["len"],
                "sources": ["response"],
            },
        ],
    )
    assert result["matched"] == 1
    assert result["strict"] == 1
    assert result["precision"] == 0.5
    assert result["recall"] == 1.0
    assert result["expression_accuracy"] == 1.0
    assert result["unexpected"][0]["target"] == "extra"


def test_score_case_reports_expression_mismatch_not_silent_match() -> None:
    result = score_case([_expected(["avg", "pluck"])], [_expected(["sum"])])
    assert result["matched"] == 1
    assert result["strict"] == 0
    assert result["expression_accuracy"] == 0.0
    assert result["expression_mismatches"]

    # 目标与运算都相同、但取值来源错了（$request 写成 $response）也不能算严格命中。
    source_wrong = score_case(
        [_expected([], ["request"])],
        [_expected([], ["response"])],
    )
    assert source_wrong["strict"] == 0
    assert source_wrong["expression_mismatches"]

    # 来源类型相同、但引用了错误的字段路径，也不能算严格命中。
    path_wrong = score_case(
        [_expected([], ["response"], ["response:total"])],
        [_expected([], ["response"], ["response:amount"])],
    )
    assert path_wrong["strict"] == 0
    assert path_wrong["expression_mismatches"]


def test_empty_expected_requires_no_prediction() -> None:
    perfect = score_case([], [])
    assert perfect["precision"] == 1.0
    assert perfect["recall"] == 1.0
    assert perfect["expression_accuracy"] == 1.0

    extra = score_case([], [_expected(["sum"])])
    assert extra["precision"] == 0.0
    assert extra["unexpected"]


def test_aggregate_is_macro_average_with_totals() -> None:
    summary = aggregate(
        [
            ("a", score_case([_expected(["sum"])], [_expected(["sum"])])),
            ("b", score_case([_expected(["avg"])], [])),
        ]
    )
    assert summary["cases"] == 2
    assert summary["precision"] == 0.5
    assert summary["recall"] == 0.5
    assert summary["expression_accuracy"] == 0.5
    assert summary["expected"] == 2
    assert summary["matched"] == 1


def test_signatures_read_ops_from_rule_relations() -> None:
    relation = RuleRelation(
        relation_key="RR-eval",
        assertion_kind="body_field",
        target="total",
        op="eq",
        expect={
            "$op": "sum",
            "args": [
                {
                    "$op": "pluck",
                    "args": [{"$response": "items"}, "price"],
                }
            ],
        },
        description="总额等于明细价格之和",
        evidence="customer_id",
    )
    rule = type("Rule", (), {"relations": [relation]})()
    assert signatures([rule]) == [
        {
            "assertion_kind": "body_field",
            "target": "total",
            "op": "eq",
            "ops": ["pluck", "sum"],
            "sources": ["response"],
            "paths": ["response:items"],
        }
    ]


def test_load_corpus_validates_shape(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "one", "requirement": "total 等于 items 数量"}
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = load_corpus(good)
    assert payload["cases"][0]["expected"] == []

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "one", "requirement": "a"},
                    {"id": "one", "requirement": "b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="id 重复"):
        load_corpus(duplicate)
