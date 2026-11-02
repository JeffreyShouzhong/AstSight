"""Agent 层确定性护栏：证据校验、绑定校验、Mock 计划、诊断与修复边界。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astsight.agent.casegen import generate_case_draft, validate_case_bindings
from astsight.agent.diagnosis import diagnose_failures
from astsight.agent.errors import AgentError
from astsight.agent.mockplan import build_mock_plan
from astsight.agent.models import (
    BehaviorDownstream,
    DownstreamBranchSpec,
    SemanticAnalysis,
    SemanticBehavior,
)
from astsight.agent.repair import propose_repairs
from astsight.agent.semantic import validate_semantics
from astsight.agent.sut import injection_env
from astsight.cases.context import build_context
from astsight.cases.draft import CaseDraft
from astsight.inventory import inventory_routes
from contracts import Assertion, MockStub, TestCase

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "downstream_service"
DOC = "docs/requirements.md"


class StubLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict[str, str]] = []

    def complete_json(self, *, system: str, user: str):
        self.calls.append({"system": system, "user": user})
        if isinstance(self.payload, list):
            return self.payload.pop(0)
        return self.payload


def _context() -> dict:
    routes, _audit, _graph, _notes = inventory_routes(
        EXAMPLE, codegraph="off", openapi_source=str(EXAMPLE / "openapi.json")
    )
    return build_context(
        EXAMPLE,
        [DOC],
        openapi=str(EXAMPLE / "openapi.json"),
        routes=routes,
    )


def _quote_section(context: dict) -> str:
    for document in context["documents"]:
        for section in document["sections"]:
            if section["section"].endswith("POST /api/v1/quotes"):
                return section["section"]
    raise AssertionError("section not found")


def _dep_id(context: dict) -> str:
    for profile in context["profiles"]:
        if profile["endpoint"] == "POST /api/v1/quotes":
            return profile["downstream_deps"][0]["dep_id"]
    raise AssertionError("dep not found")


def _analysis(context: dict) -> SemanticAnalysis:
    dep_id = _dep_id(context)
    section = _quote_section(context)
    return SemanticAnalysis(
        context_id=context["context_id"],
        behaviors=[
            SemanticBehavior(
                behavior_key="quote_success",
                endpoint="POST /api/v1/quotes",
                title="报价成功",
                category="positive",
                description="价格服务返回单价，报价总额等于单价乘数量",
                requirement_refs=[
                    {
                        "doc": DOC,
                        "section": section,
                        "quote": "报价总额等于下游返回的单价乘以数量",
                    }
                ],
                expected_status=[200],
                downstream=[
                    BehaviorDownstream(dep_id=dep_id, branch="success", why="价格存在")
                ],
            ),
            SemanticBehavior(
                behavior_key="quote_sku_missing",
                endpoint="POST /api/v1/quotes",
                title="商品不存在",
                category="business",
                description="价格服务 404 时报价接口透传 404",
                requirement_refs=[
                    {
                        "doc": DOC,
                        "section": section,
                        "quote": "当价格服务返回 404 时，报价接口返回 404",
                    }
                ],
                expected_status=[404],
                downstream=[
                    BehaviorDownstream(dep_id=dep_id, branch="not_found", why="价格不存在")
                ],
            ),
        ],
        downstream_branches=[
            DownstreamBranchSpec(
                dep_id=dep_id,
                branch="success",
                semantics="价格存在",
                stub=MockStub(status=200, body={"unit_price_cents": 2500}),
            ),
            DownstreamBranchSpec(
                dep_id=dep_id,
                branch="not_found",
                semantics="价格不存在",
                stub=MockStub(status=404, body={"error": "not found"}),
            ),
        ],
        uncovered_requirements=[],
    )


def _draft_payload(context: dict) -> dict:
    section = _quote_section(context)
    return {
        "schema_version": 1,
        "context_id": context["context_id"],
        "cases": [
            {
                "case_key": "quote_success",
                "title": "报价成功",
                "requirement_refs": [
                    {
                        "doc": DOC,
                        "section": section,
                        "quote": "报价总额等于下游返回的单价乘以数量",
                    }
                ],
                "endpoint": "POST /api/v1/quotes",
                "category": "positive",
                "body": {"sku": "ABC-1", "quantity": 2},
                "asserts": [
                    {"kind": "status_code", "op": "eq", "expect": 200},
                    {"kind": "body_field", "target": "total_cents", "op": "eq", "expect": 5000},
                ],
                "rationale": "单价 2500 × 2",
            }
        ],
    }


def test_injection_env_url_override_and_unsupported() -> None:
    document = {
        "injections": [
            {
                "dep_id": "dep-1",
                "strategy": "url_override",
                "property": "PRICING_BASE_URL",
                "injected": "http://127.0.0.1:9000/_dep/r1",
            },
            {
                "dep_id": "dep-2",
                "strategy": "unsupported",
                "property": None,
                "injected": "",
                "reason": "target_unresolved",
            },
        ]
    }
    env, keys, warnings = injection_env(document, base_url="http://127.0.0.1:8080")
    assert env["PRICING_BASE_URL"] == "http://127.0.0.1:9000/_dep/r1"
    assert "PRICING_BASE_URL" in keys
    assert any(item.startswith("injection_unsupported:dep-2") for item in warnings)


def test_semantic_validation_rejects_fake_quote_and_dep() -> None:
    context = _context()
    analysis = _analysis(context)
    ok = validate_semantics(analysis, context)
    assert ok.ok, ok.errors

    bad = analysis.model_copy(deep=True)
    bad.behaviors[0].requirement_refs[0].quote = "需求里没有这句话"
    validation = validate_semantics(bad, context)
    assert not validation.ok
    assert any("quote" in error for error in validation.errors)

    bad_dep = analysis.model_copy(deep=True)
    bad_dep.behaviors[0].downstream[0].dep_id = "dep-not-exist"
    validation = validate_semantics(bad_dep, context)
    assert not validation.ok


def test_casegen_auto_binds_single_dependency() -> None:
    context = _context()
    analysis = _analysis(context)
    draft, _validation, warnings = generate_case_draft(
        context,
        analysis,
        llm=StubLLM(_draft_payload(context)),
        project=EXAMPLE,
    )
    dep_id = _dep_id(context)
    assert draft.cases[0].mock_bindings == {dep_id: "success"}
    assert any(item.startswith("default_success_binding") for item in warnings)


def test_casegen_rejects_unknown_branch() -> None:
    context = _context()
    analysis = _analysis(context)
    payload = _draft_payload(context)
    payload["cases"][0]["mock_bindings"] = {_dep_id(context): "not_a_branch"}
    with pytest.raises(AgentError) as exc:
        generate_case_draft(
            context, analysis, llm=StubLLM(payload), project=EXAMPLE
        )
    assert exc.value.code == "casegen_validation_failed"


def test_mock_plan_uses_only_bound_branches() -> None:
    context = _context()
    analysis = _analysis(context)
    dep_id = _dep_id(context)
    cases = [
        SimpleNamespace(
            endpoint="POST /api/v1/quotes",
            case_key="quote_success",
            mock_bindings={dep_id: "success"},
        )
    ]
    plan, warnings = build_mock_plan(
        context, analysis, SimpleNamespace(cases=cases), sut_id="quote"
    )
    assert plan is not None
    assert len(plan.routes) == 1
    assert list(plan.routes[0].branches) == ["success"]
    assert plan.injections[0].strategy.value == "url_override"
    assert plan.injections[0].property == "PRICING_BASE_URL"
    assert not any("unsupported" in item for item in warnings)


def _case(case_id: str = "TC-0001", category: str = "positive") -> TestCase:
    return TestCase(
        id=case_id,
        endpoint="POST /api/v1/quotes",
        category=category,
        method="POST",
        path="/api/v1/quotes",
        body={"sku": "ABC-1", "quantity": 2},
        asserts=[Assertion(kind="status_code", op="eq", expect=200)],
        source="openapi",
    )


def _entry(case_id: str, message: str, reason: str = "case_error") -> dict:
    return {
        "case_id": case_id,
        "outcome": "failed",
        "message": message,
        "diagnosis": {"reason": reason},
    }


def _diagnosis(case_id: str, root: str = "case_error", confidence: str = "high") -> dict:
    return {
        "case_id": case_id,
        "phenomenon": "断言与需求不一致",
        "root_cause": root,
        "action_target": "user",
        "confidence": confidence,
        "evidence": [{"fact": "quote 要求 1-99", "source": "requirements.md"}],
        "verify_action": "改为期望 422",
    }


def test_diagnosis_eligibility_blocks_5xx_and_low_confidence() -> None:
    llm = StubLLM(
        [
            [
                _diagnosis("TC-0001"),
                _diagnosis("TC-0002"),
                _diagnosis("TC-0003", confidence="low"),
            ]
        ]
    )
    outcome = diagnose_failures(
        cases=[_case("TC-0001"), _case("TC-0002"), _case("TC-0003")],
        entries=[
            _entry("TC-0001", "expected 200 got 422"),
            _entry("TC-0002", "server returned 500"),
            _entry("TC-0003", "unexpected body"),
        ],
        llm=llm,
    )
    assert outcome.eligible_case_ids == {"TC-0001"}
    assert outcome.by_case["TC-0002"]["eligible_for_repair"] is False
    assert outcome.by_case["TC-0003"]["eligible_for_repair"] is False


def test_repair_rejects_forbidden_identity_change() -> None:
    context = _context()
    analysis = _analysis(context)
    draft = CaseDraft.model_validate(_draft_payload(context))
    bad_plan = {
        "schema_version": 1,
        "repairs": [
            {
                "case_key": "quote_success",
                "reason": "改 endpoint",
                "endpoint": "POST /api/v1/other",
            }
        ],
    }
    with pytest.raises(AgentError) as exc:
        propose_repairs(
            draft,
            failures=[_entry("TC-0001", "expected 200 got 500")],
            diagnoses={"TC-0001": {**_diagnosis("TC-0001"), "eligible_for_repair": True}},
            context=context,
            analysis=analysis,
            llm=StubLLM(bad_plan),
            project=EXAMPLE,
            case_keys={"TC-0001": "quote_success"},
        )
    assert exc.value.code == "repair_invalid"


def test_repair_rolls_back_invalid_patch() -> None:
    context = _context()
    analysis = _analysis(context)
    draft = CaseDraft.model_validate(_draft_payload(context))
    plan = {
        "schema_version": 1,
        "repairs": [
            {
                "case_key": "quote_success",
                "reason": "写入未知字段",
                "body": {"unknown_field": 1},
            }
        ],
    }
    outcome = propose_repairs(
        draft,
        failures=[_entry("TC-0001", "expected 200 got 500")],
        diagnoses={"TC-0001": {**_diagnosis("TC-0001"), "eligible_for_repair": True}},
        context=context,
        analysis=analysis,
        llm=StubLLM(plan),
        project=EXAMPLE,
        case_keys={"TC-0001": "quote_success"},
    )
    assert not outcome.changed
    assert outcome.rejected[0]["reason"] == "validation_failed"
    assert outcome.draft.cases[0].body == {"sku": "ABC-1", "quantity": 2}


def test_validate_case_bindings_reports_unbound_dependency() -> None:
    context = _context()
    analysis = _analysis(context)
    draft = CaseDraft.model_validate(_draft_payload(context))
    errors, warnings = validate_case_bindings(draft, analysis, context)
    assert not errors
    assert any(item.startswith("downstream_unbound") for item in warnings)
