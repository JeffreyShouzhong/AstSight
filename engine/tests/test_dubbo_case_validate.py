"""Dubbo 入口用例契约：草案校验、画像回填与跨 transport 拒绝。"""
from __future__ import annotations

from pathlib import Path

from astsight.agent.models import CasePatch, CaseStepPatch
from astsight.agent.repair import _apply_case_patch, _assertion_weakening
from astsight.cases.context import prepare_context
from astsight.cases.draft import (
    CaseDraft,
    DraftCase,
    DraftCaseStep,
    DraftRequirementRef,
)
from astsight.cases.validate import validate_draft
from contracts import (
    AssertStrength,
    Assertion,
    CaseLevel,
    DubboCall,
    Transport,
)

INTERFACE = """package com.acme;

public interface OrderService {
    OrderView byId(String id);

    String echo(String value);
}
"""

VIEW = """package com.acme;

public class OrderView {
    private String orderId;
}
"""

IMPL = """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService(group = "order", version = "1.0.0")
public class OrderServiceImpl implements OrderService {
    public OrderView byId(String id) { return null; }
    public String echo(String value) { return null; }
}
"""

DOC = "# API\n\n## 下单\n\n下单接口。\n"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    package = root / "src" / "main" / "java" / "com" / "acme"
    package.mkdir(parents=True)
    (package / "OrderService.java").write_text(INTERFACE, encoding="utf-8")
    (package / "OrderView.java").write_text(VIEW, encoding="utf-8")
    (package / "OrderServiceImpl.java").write_text(IMPL, encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(DOC, encoding="utf-8")
    return root


def _context(tmp_path: Path, *, routes: list[dict] | None = None):
    root = _project(tmp_path)
    prepared = prepare_context(
        root,
        ["docs/api.md"],
        scan_routes=False,
        routes=routes,
        out=tmp_path / "context",
    )
    return root, prepared.context


def _dubbo_profile(context: dict) -> dict:
    return next(
        item
        for item in context["profiles"]
        if item["transport"] == "dubbo" and item["method"] == "byId"
    )


def _requirement_ref(context: dict) -> DraftRequirementRef:
    section = next(
        item["section"]
        for item in context["documents"][0]["sections"]
        if item.get("include", True)
    )
    return DraftRequirementRef(
        doc="docs/api.md", section=section, quote="下单接口。"
    )


def _draft(
    context: dict,
    profile: dict,
    *,
    transport: Transport = Transport.DUBBO,
    dubbo: DubboCall | None = None,
    asserts: list[Assertion] | None = None,
    body: object = None,
    episode: str = "dubbo_by_id_ok",
) -> CaseDraft:
    return CaseDraft(
        context_id=context.get("context_id"),
        generator={"kind": "manual", "name": "test"},
        cases=[
            DraftCase(
                case_key=episode,
                level=CaseLevel.BUSINESS,
                transport=transport,
                requirement_refs=[_requirement_ref(context)],
                endpoint=profile["endpoint"],
                category="business",
                body=body,
                asserts=asserts
                or [
                    Assertion(kind="rpc_status", op="eq", expect="OK"),
                    Assertion(
                        kind="result_field", target="orderId", op="exists"
                    ),
                ],
                dubbo=(
                    dubbo
                    if dubbo is not None or transport is not Transport.DUBBO
                    else DubboCall(
                        interface="com.acme.OrderService",
                        method="byId",
                        parameter_types=["java.lang.String"],
                        arguments=["O-1"],
                        group="order",
                        version="1.0.0",
                    )
                ),
                assert_strength=AssertStrength.L1,
            )
        ],
    )


def test_dubbo_draft_is_hydrated_and_resolved(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    result = validate_draft(_draft(context, _dubbo_profile(context)), context, project=root)

    assert result.errors == []
    assert len(result.resolved) == 1
    case = result.resolved[0].case
    assert case.transport is Transport.DUBBO
    assert case.method == "byId"
    assert case.path == ""
    assert case.dubbo is not None
    assert case.dubbo.interface == "com.acme.OrderService"
    assert case.dubbo.parameter_types == ["java.lang.String"]


def test_triple_runtime_fields_are_hydrated_from_profile(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    profile = _dubbo_profile(context)
    profile.update(
        {
            "protocol": "tri",
            "serialization": "protobuf",
            "descriptor_source": "src/main/proto/inventory.proto",
            "grpc_path": "/com.acme.OrderService/byId",
            "compatibility": "grpc_compatible",
        }
    )
    draft = _draft(
        context,
        profile,
        dubbo=DubboCall(
            interface="com.acme.OrderService",
            method="byId",
            parameter_types=["java.lang.String"],
            arguments=["O-1"],
            group="order",
            version="1.0.0",
        ),
    )

    result = validate_draft(draft, context, project=root)

    assert result.errors == []
    call = result.resolved[0].case.dubbo
    assert call is not None
    assert call.serialization == "protobuf"
    assert call.descriptor_source == "src/main/proto/inventory.proto"
    assert call.grpc_path == "/com.acme.OrderService/byId"
    assert call.compatibility == "grpc_compatible"


def test_interface_mismatch_is_rejected(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    draft = _draft(
        context,
        _dubbo_profile(context),
        dubbo=DubboCall(
            interface="com.other.OtherService",
            method="byId",
            parameter_types=["java.lang.String"],
            arguments=["O-1"],
            group="order",
            version="1.0.0",
        ),
    )

    result = validate_draft(draft, context, project=root)

    assert any(
        error.code == "dubbo.interface_mismatch" for error in result.errors
    )


def test_dubbo_multistep_draft_resolves_with_refs(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    by_id = _dubbo_profile(context)
    echo = next(
        item
        for item in context["profiles"]
        if item["transport"] == "dubbo" and item["method"] == "echo"
    )
    draft = CaseDraft(
        context_id=context.get("context_id"),
        generator={"kind": "manual", "name": "test"},
        cases=[
            DraftCase(
                case_key="dubbo_flow_ok",
                level=CaseLevel.BUSINESS,
                transport=Transport.DUBBO,
                requirement_refs=[_requirement_ref(context)],
                endpoint=echo["endpoint"],
                category="business",
                asserts=[],
                steps=[
                    DraftCaseStep(
                        name="first",
                        endpoint=by_id["endpoint"],
                        dubbo=DubboCall(arguments=["O-1"]),
                        asserts=[
                            Assertion(kind="rpc_status", op="eq", expect="OK")
                        ],
                    ),
                    DraftCaseStep(
                        name="second",
                        endpoint=echo["endpoint"],
                        dubbo=DubboCall(arguments=[{"$ref": "first.orderId"}]),
                        asserts=[
                            Assertion(kind="rpc_status", op="eq", expect="OK"),
                            Assertion(kind="result_present", op="exists"),
                        ],
                    ),
                ],
                assert_strength=AssertStrength.L1,
            )
        ],
    )

    result = validate_draft(draft, context, project=root)

    assert result.errors == []
    case = result.resolved[0].case
    assert [step.transport for step in case.steps] == [
        Transport.DUBBO,
        Transport.DUBBO,
    ]
    assert case.steps[0].dubbo is not None
    assert case.steps[0].dubbo.method == "byId"
    assert case.steps[0].dubbo.arguments == ["O-1"]
    assert case.steps[1].dubbo is not None
    assert case.steps[1].dubbo.method == "echo"
    assert case.steps[1].dubbo.arguments == [{"$ref": "first.orderId"}]
    assert case.steps[1].dubbo.parameter_types == ["java.lang.String"]


def test_dubbo_step_requires_rpc_status(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    by_id = _dubbo_profile(context)
    draft = CaseDraft(
        context_id=context.get("context_id"),
        generator={"kind": "manual", "name": "test"},
        cases=[
            DraftCase(
                case_key="dubbo_flow_missing_status",
                level=CaseLevel.BUSINESS,
                transport=Transport.DUBBO,
                requirement_refs=[_requirement_ref(context)],
                endpoint=by_id["endpoint"],
                category="business",
                asserts=[],
                steps=[
                    DraftCaseStep(
                        name="first",
                        endpoint=by_id["endpoint"],
                        dubbo=DubboCall(arguments=["O-1"]),
                        asserts=[
                            Assertion(
                                kind="result_field",
                                target="orderId",
                                op="exists",
                            )
                        ],
                    )
                ],
                assert_strength=AssertStrength.L1,
            )
        ],
    )

    result = validate_draft(draft, context, project=root)
    assert any(
        error.code == "assertion.rpc_status_missing"
        for error in result.errors
    )


def test_repair_patch_can_replace_dubbo_arguments() -> None:
    patch = CasePatch(
        case_key="dubbo_by_id_ok",
        reason="修正订单号",
        dubbo=DubboCall(arguments=["O-2"]),
    )
    assert "dubbo" in patch.changed_fields()
    assert patch.dubbo is not None
    assert patch.dubbo.arguments == ["O-2"]


def _multistep_draft(context: dict) -> CaseDraft:
    by_id = _dubbo_profile(context)
    echo = next(
        item
        for item in context["profiles"]
        if item["transport"] == "dubbo" and item["method"] == "echo"
    )
    return CaseDraft(
        context_id=context.get("context_id"),
        generator={"kind": "manual", "name": "test"},
        cases=[
            DraftCase(
                case_key="dubbo_flow_ok",
                level=CaseLevel.BUSINESS,
                transport=Transport.DUBBO,
                requirement_refs=[_requirement_ref(context)],
                endpoint=echo["endpoint"],
                category="business",
                asserts=[],
                steps=[
                    DraftCaseStep(
                        name="first",
                        endpoint=by_id["endpoint"],
                        dubbo=DubboCall(arguments=["O-1"]),
                        asserts=[
                            Assertion(kind="rpc_status", op="eq", expect="OK"),
                            Assertion(
                                kind="result_field",
                                target="orderId",
                                op="exists",
                            ),
                        ],
                    ),
                    DraftCaseStep(
                        name="second",
                        endpoint=echo["endpoint"],
                        dubbo=DubboCall(arguments=[{"$ref": "first.orderId"}]),
                        asserts=[
                            Assertion(kind="rpc_status", op="eq", expect="OK"),
                            Assertion(kind="result_present", op="exists"),
                        ],
                    ),
                ],
                assert_strength=AssertStrength.L1,
            )
        ],
    )


def test_repair_step_patch_applies_and_validates(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    draft = _multistep_draft(context)
    target = draft.cases[0]
    patch = CasePatch(
        case_key="dubbo_flow_ok",
        reason="修正第二步参数",
        steps=[
            CaseStepPatch(
                name="second",
                dubbo=DubboCall(arguments=["O-2"]),
            )
        ],
    )

    assert _assertion_weakening(target, patch) is None
    assert _apply_case_patch(target, patch) is None
    assert target.steps[1].dubbo is not None
    assert target.steps[1].dubbo.arguments == ["O-2"]

    result = validate_draft(draft, context, project=root)
    assert result.errors == []


def test_repair_step_patch_rejects_unknown_step(tmp_path: Path) -> None:
    _root, context = _context(tmp_path)
    draft = _multistep_draft(context)
    target = draft.cases[0]
    patch = CasePatch(
        case_key="dubbo_flow_ok",
        reason="错误步骤",
        steps=[
            CaseStepPatch(
                name="missing",
                dubbo=DubboCall(arguments=["O-2"]),
            )
        ],
    )

    assert _apply_case_patch(target, patch) == "step_unknown:missing"


def test_repair_step_patch_guards_assertions(tmp_path: Path) -> None:
    _root, context = _context(tmp_path)
    draft = _multistep_draft(context)
    target = draft.cases[0]

    fewer = CasePatch(
        case_key="dubbo_flow_ok",
        reason="删断言",
        steps=[
            CaseStepPatch(
                name="second",
                asserts=[Assertion(kind="result_present", op="exists")],
            )
        ],
    )
    assert _assertion_weakening(target, fewer).startswith(
        "step_assertion_count_decreased"
    )

    missing_status = CasePatch(
        case_key="dubbo_flow_ok",
        reason="缺 rpc_status",
        steps=[
            CaseStepPatch(
                name="second",
                asserts=[
                    Assertion(
                        kind="result_field",
                        target="message",
                        op="exists",
                    ),
                    Assertion(kind="result_present", op="exists"),
                ],
            )
        ],
    )
    assert _assertion_weakening(target, missing_status) == (
        "step_rpc_status_missing:second"
    )


def test_http_fields_are_rejected_for_dubbo(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    draft = _draft(context, _dubbo_profile(context), body={"id": "O-1"})

    result = validate_draft(draft, context, project=root)

    assert any(
        error.code == "dubbo.http_fields_present" for error in result.errors
    )


def test_dubbo_case_requires_rpc_status_assertion(tmp_path: Path) -> None:
    root, context = _context(tmp_path)
    draft = _draft(
        context,
        _dubbo_profile(context),
        asserts=[
            Assertion(kind="result_field", target="orderId", op="exists")
        ],
    )

    result = validate_draft(draft, context, project=root)

    assert any(
        error.code == "assertion.rpc_status_missing"
        for error in result.errors
    )


def test_dubbo_assertion_on_http_case_is_rejected(tmp_path: Path) -> None:
    root, context = _context(
        tmp_path,
        routes=[
            {
                "method": "POST",
                "path": "/orders",
                "handler": "createOrder",
                "source": "framework_plugin",
                "confidence": "verified",
            }
        ],
    )
    http_profile = next(
        item for item in context["profiles"] if item["transport"] == "http"
    )
    draft = _draft(
        context,
        http_profile,
        transport=Transport.HTTP,
        asserts=[Assertion(kind="rpc_status", op="eq", expect="OK")],
        episode="http_with_dubbo_assert",
    )

    result = validate_draft(draft, context, project=root)

    assert any(
        error.code == "assertion.transport_mismatch"
        for error in result.errors
    )
