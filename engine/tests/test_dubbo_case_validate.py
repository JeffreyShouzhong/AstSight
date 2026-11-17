"""Dubbo 入口用例契约：草案校验、画像回填与跨 transport 拒绝。"""
from __future__ import annotations

from pathlib import Path

from astsight.cases.context import prepare_context
from astsight.cases.draft import CaseDraft, DraftCase, DraftRequirementRef
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
