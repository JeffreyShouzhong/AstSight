"""用例 fingerprint v2：行为/归属/前置/规则进指纹，change_refs 不进。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from astsight.cases.draft import case_fingerprint
from contracts import TestCase as CaseModel


def _base() -> dict[str, Any]:
    return {
        "id": "TC-0001",
        "case_key": "order_create_ok",
        "title": "创建订单",
        "endpoint": "POST /orders",
        "category": "positive",
        "level": "business",
        "transport": "http",
        "coverage_role": "primary",
        "method": "POST",
        "path": "/orders",
        "source": "llm",
        "body": {"quantity": 1},
        "asserts": [
            {"kind": "status_code", "op": "eq", "expect": 201},
            {"kind": "body_field", "target": "order_id", "op": "exists", "expect": True},
        ],
        "requirement_refs": [
            {"doc": "docs/req.md", "section": "下单", "quote": "下单成功后返回订单号"}
        ],
        "rule_refs": [
            {"rule_key": "RK-aaaaaaaaaaaa", "rule_id": "BR-01", "authority": "A"}
        ],
        "change_refs": [],
        "preconditions": [
            {"kind": "db_state", "state_key": "order_empty"}
        ],
        "mock_bindings": {"inventory": "success"},
    }


def _fp(*, case_key: str = "order_create_ok", **overrides: Any) -> str:
    data = deepcopy(_base())
    data.pop("case_key")
    data.update(overrides)
    return case_fingerprint(CaseModel.model_validate(data), case_key)


def test_fingerprint_is_stable_for_presentation_only_changes() -> None:
    baseline = _fp()
    assert _fp(id="TC-9999") == baseline
    assert _fp(title="另一个标题") == baseline
    assert _fp(source="human") == baseline
    assert _fp(change_refs=[{"base": "main", "target": "feat", "symbols": ["OrderService.create"]}]) == baseline


def test_fingerprint_tracks_level_transport_coverage() -> None:
    baseline = _fp()
    assert _fp(level="code") != baseline
    assert _fp(transport="grpc") != baseline
    assert _fp(coverage_role="alias") != baseline


def test_fingerprint_tracks_rules_preconditions_and_mock_bindings() -> None:
    baseline = _fp()
    assert _fp(rule_refs=[{"rule_key": "RK-bbbbbbbbbbbb", "rule_id": "BR-02"}]) != baseline
    assert _fp(preconditions=[{"kind": "db_state", "state_key": "order_paid"}]) != baseline
    assert _fp(mock_bindings={"inventory": "timeout"}) != baseline
    assert _fp(mock_bindings={"inventory": "success", "payment": "error"}) != baseline


def test_fingerprint_tracks_execution_and_requirement_quote() -> None:
    baseline = _fp()
    assert _fp(body={"quantity": 2}) != baseline
    assert _fp(
        asserts=[
            {"kind": "status_code", "op": "eq", "expect": 200},
            {"kind": "body_field", "target": "order_id", "op": "exists", "expect": True},
        ]
    ) != baseline
    assert _fp(
        requirement_refs=[
            {"doc": "docs/req.md", "section": "下单", "quote": "下单失败时返回错误码"}
        ]
    ) != baseline


def test_fingerprint_changes_with_case_key() -> None:
    assert _fp(case_key="order_create_alt") != _fp()
