"""C1 ContractModel：四源能力子集、gap 与确定性 CR。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from astsight.rules.context import _openapi_reference_gaps, build_rules_context
from astsight.rules.contract import build_contract_rules
from astsight.rules.contract_model import build_contract_model
from astsight.rules.draft import RuleDraft
from astsight.rules.store import import_rule_draft


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root: Path) -> None:
    _write(
        root,
        "docs/requirements.md",
        "# 订单\n\n创建订单必须校验客户名称和数量。\n",
    )
    _write(
        root,
        "src/main/java/com/acme/CreateOrderRequest.java",
        """package com.acme;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public class CreateOrderRequest {
    @NotNull
    @Size(min = 2, max = 20)
    private String customerName;
    @Min(1)
    private int quantity;
}
""",
    )
    _write(
        root,
        "src/main/java/com/acme/OrderView.java",
        """package com.acme;

public class OrderView {
    private String orderId;
}
""",
    )
    _write(
        root,
        "src/main/java/com/acme/OrderController.java",
        """package com.acme;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class OrderController {
    @PostMapping("/orders")
    public ResponseEntity<OrderView> create(
        @PathVariable("tenant") String tenant,
        @RequestParam(name = "dryRun", required = false) boolean dryRun,
        @RequestBody CreateOrderRequest request
    ) {
        return null;
    }
}
""",
    )
    _write(
        root,
        "src/main/java/com/acme/OrderService.java",
        """package com.acme;

public interface OrderService {
    OrderView create(CreateOrderRequest request);
}
""",
    )
    _write(
        root,
        "src/main/java/com/acme/OrderServiceImpl.java",
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService(group = "orders", version = "1.0.0")
public class OrderServiceImpl implements OrderService {
    public OrderView create(CreateOrderRequest request) {
        return null;
    }
}
""",
    )
    _write(
        root,
        "proto/order.proto",
        """syntax = "proto3";
package acme.order.v1;

enum OrderState {
  ORDER_STATE_UNSPECIFIED = 0;
  ORDER_STATE_CREATED = 1;
}

message CreateOrderRequest {
  string customer_name = 1;
  optional int32 quantity = 2;
  repeated string tags = 3;
  oneof contact {
    string email = 4;
    string phone = 5;
  }
}

message CreateOrderResponse {
  string order_id = 1;
  OrderState state = 2;
}

service OrderService {
  rpc Create(CreateOrderRequest) returns (CreateOrderResponse);
}
""",
    )
    _write(
        root,
        "src/main/python/server.py",
        """def serve(server):
    add_OrderServiceServicer_to_server(OrderService(), server)
""",
    )
    _write(root, "openapi.json", "{}")


def _openapi_operations() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            endpoint="POST /api/orders",
            operation_id="createOrder",
            params=[
                SimpleNamespace(
                    name="tenant",
                    location="path",
                    type="string",
                    required=True,
                    constraints={},
                )
            ],
            body_schema={
                "type": "object",
                "required": ["customerName"],
                "properties": {
                    "customerName": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 20,
                    },
                    "quantity": {"type": "integer", "minimum": 1},
                },
            },
            success_codes=[200],
            response_fields={},
        )
    ]


def test_contract_model_normalises_four_sources_and_gaps(tmp_path: Path) -> None:
    _project(tmp_path)

    first = build_contract_model(
        tmp_path,
        openapi_operations=_openapi_operations(),
        openapi_source="openapi.json",
        openapi_version="sha-openapi",
    )
    second = build_contract_model(
        tmp_path,
        openapi_operations=_openapi_operations(),
        openapi_source="openapi.json",
        openapi_version="sha-openapi",
    )

    assert first.as_dict() == second.as_dict()
    assert first.content_hash() == second.content_hash()
    assert {item["kind"] for item in first.sources} == {
        "openapi",
        "spring_mvc",
        "grpc_descriptor",
        "dubbo_interface",
    }

    by_source: dict[str, list] = {}
    for operation in first.operations:
        by_source.setdefault(operation.source_kind, []).append(operation)

    spring = by_source["spring_mvc"][0]
    spring_request = {item.path: item for item in spring.request}
    assert spring_request["path.tenant"].required is True
    assert spring_request["query.dryRun"].required is False
    assert spring_request["body.customerName"].required is True
    assert spring_request["body.customerName"].constraints == {
        "minLength": 2,
        "maxLength": 20,
    }
    assert spring_request["body.quantity"].constraints == {"minimum": 1}
    assert spring.response[0].path == "response.orderId"

    grpc = next(
        item
        for item in by_source["grpc_descriptor"]
        if item.endpoint.endswith(".OrderService/Create")
    )
    grpc_request = {item.path: item for item in grpc.request}
    assert grpc_request["request.customerName"].type == "string"
    assert grpc_request["request.quantity"].required is False
    assert grpc_request["request.tags"].constraints["repeated"] is True
    assert grpc_request["request.email"].constraints["oneof"] == "contact"
    grpc_response = {item.path: item for item in grpc.response}
    assert grpc_response["response.state"].constraints["enum"] == [
        "ORDER_STATE_UNSPECIFIED",
        "ORDER_STATE_CREATED",
    ]

    dubbo = by_source["dubbo_interface"][0]
    dubbo_request = {item.path: item for item in dubbo.request}
    assert dubbo.transport == "dubbo"
    assert "request.arg0.customerName" in dubbo_request
    assert "response.result.orderId" in {
        item.path for item in dubbo.response
    }

    gap_codes = {item.code for item in first.gaps}
    assert "spring_mvc_runtime_semantics_unverified" in gap_codes
    assert "grpc_descriptor_runtime_semantics_unverified" in gap_codes
    assert "dubbo_interface_runtime_semantics_unverified" in gap_codes
    assert "openapi_runtime_semantics_unverified" in gap_codes
    assert "grpc_provider_registration_unverified" not in gap_codes


def test_contract_rules_merge_support_and_ruleset_keeps_gaps(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    model = build_contract_model(
        tmp_path,
        openapi_operations=_openapi_operations(),
        openapi_source="openapi.json",
        openapi_version="sha-openapi",
    )
    rules = build_contract_rules([], contract_model=model)
    by_field = {
        (rule.endpoint, rule.field, rule.constraint): rule for rule in rules
    }

    tenant = by_field[
        ("POST /api/orders", "path.tenant", "type=string; required=True")
    ]
    assert {item["source_kind"] for item in tenant.support} == {
        "openapi",
        "spring_mvc",
    }
    assert any(
        rule.field == "response.orderId"
        and rule.transport.value == "http"
        and rule.source_kind == "spring_mvc"
        for rule in rules
    )
    assert all(rule.support for rule in rules)

    context = build_rules_context(
        tmp_path,
        ["docs/requirements.md"],
        openapi="openapi.json",
    )
    context_again = build_rules_context(
        tmp_path,
        ["docs/requirements.md"],
        openapi="openapi.json",
    )
    assert context["schema_version"] == 2
    assert context["context_id"] == context_again["context_id"]
    assert (
        context["contract_summary"]["model_hash"]
        == context_again["contract_summary"]["model_hash"]
    )
    context_ops = {
        (item["source_kind"], item["endpoint"])
        for item in context["contract_model"]["operations"]
    }
    assert context_ops == {
        (item.source_kind, item.endpoint)
        for item in model.operations
        if item.source_kind != "openapi"
    }
    assert context["contract_summary"]["counts"]["gaps"] > 0

    draft = RuleDraft.model_validate(
        {
            "schema_version": 1,
            "context_id": context["context_id"],
            "rules": [],
            "gaps": [],
            "questions": [],
            "conflicts": [],
        }
    )
    imported = import_rule_draft(
        project=tmp_path.name,
        context=context,
        draft=draft,
        path=tmp_path / "ruleset.json",
    )
    assert imported.written
    assert imported.ruleset.contract_rules
    assert any(item.kind == "contract" for item in imported.ruleset.gaps)
    assert any(item.source_kind == "spring_mvc" for item in imported.ruleset.contract_rules)


def test_grpc_descriptor_without_provider_does_not_create_cr(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    (tmp_path / "src/main/python/server.py").unlink()

    model = build_contract_model(tmp_path)
    grpc = next(
        item
        for item in model.operations
        if item.source_kind == "grpc_descriptor"
    )
    assert grpc.contract_eligible is False
    assert any(
        gap.code == "grpc_provider_registration_unverified"
        for gap in model.gaps
    )
    assert not any(
        rule.transport.value == "grpc"
        for rule in build_contract_rules([], contract_model=model)
    )


def test_openapi_unresolved_refs_become_contract_gaps() -> None:
    spec = {
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "https://contracts.example/order.json"
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Missing"}
                                }
                            }
                        }
                    },
                }
            }
        }
    }
    gaps = _openapi_reference_gaps(spec, "openapi.json")
    assert {item.code for item in gaps} == {
        "openapi_external_ref_unresolved",
        "openapi_local_ref_unresolved",
    }
    assert {item.endpoint for item in gaps} == {"POST /orders"}


def test_cross_source_contract_conflict_is_preserved(tmp_path: Path) -> None:
    _project(tmp_path)
    operations = _openapi_operations()
    operations[0].body_schema["properties"]["quantity"]["maximum"] = 99
    model = build_contract_model(
        tmp_path,
        openapi_operations=operations,
        openapi_source="openapi.json",
        openapi_version="sha-openapi",
    )
    context = build_rules_context(
        tmp_path,
        ["docs/requirements.md"],
        openapi="openapi.json",
    )
    context["contract_model"] = model.as_dict()
    draft = RuleDraft.model_validate(
        {
            "schema_version": 1,
            "context_id": context["context_id"],
            "rules": [],
            "gaps": [],
            "questions": [],
            "conflicts": [],
        }
    )
    imported = import_rule_draft(
        project=tmp_path.name,
        context=context,
        draft=draft,
    )
    conflict = next(
        item
        for item in imported.ruleset.conflicts
        if "#body.quantity" in item.topic
    )
    assert conflict.origin == "framework"
    assert len(conflict.rule_keys) == 2
    assert "openapi" in conflict.reason
    assert "spring_mvc" in conflict.reason

