"""离线评估：规则关系（公式）抽取质量。

评估只比较「目标字段 + 关系运算 + 表达式运算集合 + 取值来源
（response/request/step）」，不比较 JSON 的具体嵌套形态，
因为同一公式可以写成不同但等价的树。这个模块不调用模型；调用方负责拿到
`Rule.relations`，这里只做打分，便于在提示词/模型变更后做回归对比。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .rules.relations import iter_relation_nodes, relation_dynamic_kind


def relation_signature(relation: Any) -> dict[str, Any]:
    ops: list[str] = []
    sources: set[str] = set()
    for value in (getattr(relation, "expect", None), getattr(relation, "where", None)):
        try:
            nodes = list(iter_relation_nodes(value))
        except ValueError:
            continue
        for node, _where in nodes:
            kind = relation_dynamic_kind(node)
            if kind in {"response", "request", "step"}:
                sources.add(kind)
                continue
            if kind != "op":
                continue
            op = str(node.get("$op") or "")
            if op and op not in ops:
                ops.append(op)
    return {
        "assertion_kind": str(getattr(relation, "assertion_kind", "")),
        "target": str(getattr(relation, "target", "")),
        "op": str(getattr(relation, "op", "")),
        "ops": sorted(ops),
        "sources": sorted(sources),
    }


def signatures(rules: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rule in rules:
        for relation in getattr(rule, "relations", []) or []:
            out.append(relation_signature(relation))
    return out


def _key(signature: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(signature.get("assertion_kind") or "body_field"),
        str(signature.get("target") or ""),
        str(signature.get("op") or ""),
    )


def score_case(
    expected: Iterable[dict[str, Any]],
    predicted: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    expected_list = [dict(item) for item in expected]
    predicted_list = [dict(item) for item in predicted]
    used = [False] * len(predicted_list)
    matched = 0
    strict = 0
    mismatches: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    for want in expected_list:
        key = _key(want)
        index = next(
            (
                position
                for position, item in enumerate(predicted_list)
                if not used[position] and _key(item) == key
            ),
            None,
        )
        if index is None:
            missed.append(want)
            continue
        used[index] = True
        matched += 1
        got = predicted_list[index]
        if (
            sorted(got.get("ops") or []) == sorted(want.get("ops") or [])
            and sorted(got.get("sources") or [])
            == sorted(want.get("sources") or [])
        ):
            strict += 1
        else:
            mismatches.append({"expected": want, "predicted": got})
    unexpected = [
        item for position, item in enumerate(predicted_list) if not used[position]
    ]
    expected_total = len(expected_list)
    predicted_total = len(predicted_list)
    precision = (
        1.0
        if predicted_total == 0 and expected_total == 0
        else (matched / predicted_total if predicted_total else 0.0)
    )
    recall = 1.0 if expected_total == 0 else matched / expected_total
    expression_accuracy = (
        strict / matched if matched else (1.0 if not expected_list else 0.0)
    )
    return {
        "expected": expected_total,
        "predicted": predicted_total,
        "matched": matched,
        "strict": strict,
        "precision": precision,
        "recall": recall,
        "expression_accuracy": expression_accuracy,
        "missed": missed,
        "unexpected": unexpected,
        "expression_mismatches": mismatches,
    }


def aggregate(cases: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    items = list(cases)
    if not items:
        return {
            "cases": 0,
            "precision": 0.0,
            "recall": 0.0,
            "expression_accuracy": 0.0,
            "expected": 0,
            "predicted": 0,
            "matched": 0,
        }
    count = len(items)
    expected_total = sum(result["expected"] for _name, result in items)
    predicted_total = sum(result["predicted"] for _name, result in items)
    matched_total = sum(result["matched"] for _name, result in items)
    strict_total = sum(result["strict"] for _name, result in items)
    return {
        "cases": count,
        "precision": round(
            sum(result["precision"] for _name, result in items) / count, 4
        ),
        "recall": round(
            sum(result["recall"] for _name, result in items) / count, 4
        ),
        "expression_accuracy": round(
            sum(result["expression_accuracy"] for _name, result in items) / count, 4
        ),
        "expected": expected_total,
        "predicted": predicted_total,
        "matched": matched_total,
        "strict": strict_total,
        "unexpected": sum(
            len(result["unexpected"]) for _name, result in items
        ),
    }


def load_corpus(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{resolved}: 评估集顶层必须是对象")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{resolved}: cases 不能为空")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"{resolved}: cases[{index}] 必须是对象")
        case_id = str(case.get("id") or "")
        if not case_id:
            raise ValueError(f"{resolved}: cases[{index}] 缺少 id")
        if case_id in seen:
            raise ValueError(f"{resolved}: id 重复: {case_id}")
        seen.add(case_id)
        if not str(case.get("requirement") or "").strip():
            raise ValueError(f"{resolved}: {case_id} 缺少 requirement")
        expected = case.get("expected")
        if expected is None:
            case["expected"] = []
        elif not isinstance(expected, list):
            raise ValueError(f"{resolved}: {case_id}.expected 必须是列表")
    return payload


__all__ = [
    "aggregate",
    "load_corpus",
    "relation_signature",
    "score_case",
    "signatures",
]
