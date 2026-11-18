"""Dubbo 提供方（@DubboService / XML）画像扫描。"""
from __future__ import annotations

from pathlib import Path

from astsight.cases.context import prepare_context
from astsight.cases.templates import generate_cases
from astsight.profile.builder import build_profiles
from astsight.profile.dubbo import scan_dubbo_sources
from contracts import Transport

INTERFACE = """package com.acme;

public interface OrderService {
    OrderView create(CreateOrderRequest request);

    OrderView byId(String id);
}
"""

DTO = """package com.acme;

public class CreateOrderRequest {
    private String sku;
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
    public OrderView create(CreateOrderRequest request) { return null; }

    public OrderView byId(String id) { return null; }
}
"""

CREATE_ENDPOINT = (
    "dubbo:com.acme.OrderService#create(com.acme.CreateOrderRequest)"
    "?group=order&version=1.0.0"
)
BY_ID_ENDPOINT = (
    "dubbo:com.acme.OrderService#byId(java.lang.String)"
    "?group=order&version=1.0.0"
)


def _write(root: Path, name: str, text: str) -> Path:
    path = root / "src" / "main" / "java" / "com" / "acme" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _project(tmp_path: Path) -> Path:
    _write(tmp_path, "OrderService.java", INTERFACE)
    _write(tmp_path, "CreateOrderRequest.java", DTO)
    _write(tmp_path, "OrderView.java", VIEW)
    return tmp_path


def _scan(root: Path):
    files = [
        (path, "")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return scan_dubbo_sources(
        files=files,
        root=root,
        read_text=lambda path: path.read_text(encoding="utf-8"),
    )


def test_annotation_provider_emits_method_profiles(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "OrderServiceImpl.java", IMPL)

    result = _scan(root)

    assert [item["endpoint"] for item in result.providers] == [
        BY_ID_ENDPOINT,
        CREATE_ENDPOINT,
    ]
    create = next(item for item in result.providers if item["method"] == "create")
    assert create["transport"] == "dubbo"
    assert create["service"] == "com.acme.OrderService"
    assert create["parameter_types"] == ["com.acme.CreateOrderRequest"]
    assert create["return_type"] == "com.acme.OrderView"
    assert create["group"] == "order"
    assert create["version"] == "1.0.0"
    assert create["impl_class"] == "com.acme.OrderServiceImpl"
    assert create["business_key"] == "com.acme.OrderServiceImpl#create"
    assert create["contract_incomplete"] is False
    assert create["line"] > 0


def test_interface_name_override_and_class_literal(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root,
        "RenamedServiceImpl.java",
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService(interfaceName = "com.acme.OrderService", group = "g")
public class RenamedServiceImpl implements Marker {
    public OrderView create(CreateOrderRequest request) { return null; }
}
""",
    )
    _write(
        root,
        "LiteralServiceImpl.java",
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService(interfaceClass = OrderService.class, version = "2")
public class LiteralServiceImpl {
    public OrderView byId(String id) { return null; }
}
""",
    )
    _write(root, "Marker.java", "package com.acme;\npublic interface Marker {}\n")

    result = _scan(root)

    endpoints = {item["endpoint"] for item in result.providers}
    assert (
        "dubbo:com.acme.OrderService#byId(java.lang.String)?group=g"
        in endpoints
    )
    assert (
        "dubbo:com.acme.OrderService#byId(java.lang.String)?version=2"
        in endpoints
    )


def test_multiple_interfaces_without_override_is_ambiguous(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "OtherService.java", "package com.acme;\npublic interface OtherService {}\n")
    _write(
        root,
        "BothServiceImpl.java",
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService
public class BothServiceImpl implements OrderService, OtherService {
    public OrderView create(CreateOrderRequest request) { return null; }
    public OrderView byId(String id) { return null; }
}
""",
    )

    result = _scan(root)

    assert result.providers == []
    assert result.unsupported[0]["reason"] == "dubbo_provider_ambiguous"
    assert result.unsupported[0]["kind"] == "provider"


def test_dubbo_2_6_annotation_is_explicitly_unsupported(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root,
        "LegacyServiceImpl.java",
        """package com.acme;

import com.alibaba.dubbo.config.annotation.Service;

@Service
public class LegacyServiceImpl implements OrderService {
    public OrderView create(CreateOrderRequest request) { return null; }
    public OrderView byId(String id) { return null; }
}
""",
    )

    result = _scan(root)

    assert result.providers == []
    assert result.unsupported[0]["reason"] == "dubbo_2_6_unsupported"


def test_spring_service_is_not_mistaken_for_dubbo(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root,
        "SpringServiceImpl.java",
        """package com.acme;

import org.springframework.stereotype.Service;

@Service
public class SpringServiceImpl implements OrderService {
    public OrderView create(CreateOrderRequest request) { return null; }
    public OrderView byId(String id) { return null; }
}
""",
    )

    result = _scan(root)

    assert result.providers == []
    assert result.unsupported == []


def test_interface_in_jar_is_unsupported(tmp_path: Path) -> None:
    root = tmp_path
    _write(
        root,
        "ExternalServiceImpl.java",
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService
public class ExternalServiceImpl implements ExternalService {
}
""",
    )

    result = _scan(root)

    assert result.providers == []
    assert result.unsupported[0]["reason"] == "dubbo_interface_in_jar"


def test_triple_provider_uses_proto_contract_when_interface_is_generated(
    tmp_path: Path,
) -> None:
    proto = tmp_path / "src" / "main" / "proto" / "inventory.proto"
    proto.parent.mkdir(parents=True)
    proto.write_text(
        """
        syntax = "proto3";
        package com.acme.triple;
        message ReserveRequest { string sku = 1; }
        message ReserveResponse {
          bool available = 1;
          string reservation_id = 2;
        }
        service InventoryService {
          rpc Reserve(ReserveRequest) returns (ReserveResponse);
        }
        """,
        encoding="utf-8",
    )
    _write(
        tmp_path,
        "InventoryServiceImpl.java",
        """package com.acme.triple;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService(
    interfaceClass = InventoryService.class,
    url = "tri://127.0.0.1:50053",
    group = "inventory",
    version = "1.0.0"
)
public class InventoryServiceImpl
        extends DubboInventoryServiceTriple.InventoryServiceImplBase {
}
""",
    )

    result = _scan(tmp_path)

    assert len(result.providers) == 1
    provider = result.providers[0]
    assert provider["service"] == "com.acme.triple.InventoryService"
    assert provider["method"] == "Reserve"
    assert provider["protocol"] == "tri"
    assert provider["serialization"] == "protobuf"
    assert provider["compatibility"] == "grpc_compatible"
    assert provider["grpc_path"] == (
        "/com.acme.triple.InventoryService/Reserve"
    )
    assert provider["descriptor_source"] == "src/main/proto/inventory.proto"


def test_xml_service_emits_profiles(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "dubbo-provider.xml").write_text(
        '<dubbo:service interface="com.acme.OrderService" ref="orderService" '
        'group="order" version="1.0.0"/>',
        encoding="utf-8",
    )

    result = _scan(root)

    assert [item["endpoint"] for item in result.providers] == [
        BY_ID_ENDPOINT,
        CREATE_ENDPOINT,
    ]
    assert result.providers[0]["impl_class"] == "orderService"
    assert result.providers[0]["file"] == "dubbo-provider.xml"


def test_kotlin_provider_is_unsupported(tmp_path: Path) -> None:
    root = _project(tmp_path)
    path = root / "src" / "main" / "kotlin" / "OrderService.kt"
    path.parent.mkdir(parents=True)
    path.write_text(
        "import org.apache.dubbo.config.annotation.DubboService\n"
        "@DubboService\n"
        "class OrderServiceImpl : OrderService\n",
        encoding="utf-8",
    )

    result = _scan(root)

    assert any(
        item["reason"] == "dubbo_kotlin_provider_unsupported"
        for item in result.unsupported
    )


def test_build_profiles_appends_dubbo_profiles_and_template_skips_them(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _write(root, "OrderServiceImpl.java", IMPL)

    profiles, unmapped = build_profiles([], [], project_root=root)

    dubbo = [item for item in profiles if item["transport"] == "dubbo"]
    assert {item["endpoint"] for item in dubbo} == {
        BY_ID_ENDPOINT,
        CREATE_ENDPOINT,
    }
    assert all(item["method"] != "GET" for item in dubbo)
    assert all(item["path"] == "" for item in dubbo)
    assert unmapped == []
    cases = generate_cases(profiles)
    assert len(cases) == 2
    assert all(case.transport is Transport.DUBBO for case in cases)
    assert {case.dubbo.method for case in cases if case.dubbo} == {"create", "byId"}
    assert all(case.path == "" for case in cases)
    assert all(case.endpoint.startswith("dubbo:") for case in cases)


def test_dto_fields_enum_and_nested_dto_are_sampled(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root,
        "CreateOrderRequest.java",
        """package com.acme;

import java.util.List;

public class CreateOrderRequest {
    @NotNull
    private String sku;
    private int quantity;
    private OrderStatus status;
    private List<String> tags;
    private Address address;
}
""",
    )
    _write(
        root,
        "OrderStatus.java",
        "package com.acme;\npublic enum OrderStatus { NEW, PAID }\n",
    )
    _write(
        root,
        "Address.java",
        "package com.acme;\npublic class Address { private String city; }\n",
    )
    _write(root, "OrderServiceImpl.java", IMPL)

    result = _scan(root)
    create = next(item for item in result.providers if item["method"] == "create")
    schema = create["parameter_schemas"][0]
    assert schema["kind"] == "dto"
    fields = {field["name"]: field for field in schema["fields"]}
    assert fields["sku"]["required"] is True
    assert fields["quantity"]["schema"]["kind"] == "scalar"
    assert fields["status"]["schema"]["kind"] == "enum"
    assert fields["status"]["schema"]["enum_values"] == ["NEW", "PAID"]
    assert fields["tags"]["schema"]["kind"] == "collection"
    assert fields["tags"]["schema"]["element"]["type"] == "java.lang.String"
    assert fields["address"]["schema"]["kind"] == "dto"
    assert fields["address"]["schema"]["fields"][0]["name"] == "city"
    assert create["result_schema"]["kind"] == "dto"

    profiles, _ = build_profiles([], [], project_root=root)
    cases = generate_cases(profiles)
    case = next(
        item
        for item in cases
        if item.dubbo is not None and item.dubbo.method == "create"
    )
    assert case.dubbo.arguments == [
        {
            "sku": "sample",
            "quantity": 1,
            "status": "NEW",
            "tags": ["sample"],
            "address": {"city": "sample"},
        }
    ]
    assert case.note is None


def test_record_components_are_parsed_as_fields(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root,
        "CreateOrderRequest.java",
        "package com.acme;\n"
        "public record CreateOrderRequest(String sku, int quantity) {}\n",
    )
    _write(root, "OrderServiceImpl.java", IMPL)

    result = _scan(root)
    create = next(item for item in result.providers if item["method"] == "create")
    fields = {
        field["name"]: field
        for field in create["parameter_schemas"][0]["fields"]
    }
    assert set(fields) == {"sku", "quantity"}
    assert fields["sku"]["required"] is True


def test_a4_prompt_includes_dubbo_profiles_and_dto_fields(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _write(root, "OrderServiceImpl.java", IMPL)
    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text("# API\n\n## 下单\n\n下单接口。\n", encoding="utf-8")
    prepared = prepare_context(
        root,
        ["docs/api.md"],
        scan_routes=False,
        out=tmp_path / "context",
    )

    from astsight.agent.case_design import _prompt_input
    from contracts import RuleSet

    payload = _prompt_input(
        prepared.context,
        ruleset=RuleSet(),
        mode="full",
        changed_apis=[],
        mock_semantic=None,
        data_catalog=None,
    )

    dubbo = [
        item for item in payload["profiles"] if item["transport"] == "dubbo"
    ]
    assert dubbo
    by_id = next(item for item in dubbo if item["method"] == "byId")
    assert by_id["service"] == "com.acme.OrderService"
    assert by_id["parameter_schemas"][0]["kind"] == "scalar"
    assert payload["unsupported_profiles"] == []


def test_constraints_temporal_collections_and_opaque_types_are_sampled(
    tmp_path: Path,
) -> None:
    root = tmp_path
    package = root / "src" / "main" / "java" / "com" / "acme"
    package.mkdir(parents=True)
    (package / "RichService.java").write_text(
        "package com.acme;\n"
        "public interface RichService {\n"
        "    String rich(RichRequest request);\n"
        "}\n",
        encoding="utf-8",
    )
    (package / "RichRequest.java").write_text(
        """package com.acme;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

public class RichRequest {
    @NotNull
    @Size(min = 3, max = 4)
    private String code;
    @Min(10)
    @Max(20)
    private int quantity;
    @Email
    private String email;
    @Pattern(regexp = "^[A-Z]{2}$")
    private String region;
    private LocalDate birthday;
    private List<Address> addresses;
    private Map<String, Address> byCity;
    private String[] tags;
    @Mobile
    private String phone;
    @Range(min = 1, max = 9)
    private int level;
    @EnumValue(values = {"A", "B"})
    private String grade;
    @JsonProperty("ignored")
    private String ignored;
    @BizCheck(code = "X")
    private String biz;
    @Size(min = 5)
    private List<String> five;
    @Size(min = 12)
    private List<String> many;
    @Pattern(regexp = "^(?=a)a$")
    private String lookahead;
    private UnknownThirdParty external;
}
""",
        encoding="utf-8",
    )
    (package / "Address.java").write_text(
        "package com.acme;\npublic class Address { private String city; }\n",
        encoding="utf-8",
    )
    (package / "RichServiceImpl.java").write_text(
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService(group = "rich", version = "1")
public class RichServiceImpl implements RichService {
    public String rich(RichRequest request) { return null; }
}
""",
        encoding="utf-8",
    )

    result = _scan(root)
    provider = next(
        item for item in result.providers if item["method"] == "rich"
    )
    schema = provider["parameter_schemas"][0]
    fields = {field["name"]: field for field in schema["fields"]}
    assert fields["code"]["constraints"] == {
        "required": True,
        "min_length": 3,
        "max_length": 4,
    }
    assert fields["quantity"]["constraints"] == {"minimum": 10, "maximum": 20}
    assert fields["email"]["constraints"] == {"format": "email"}
    assert fields["region"]["constraints"] == {"pattern": "^[A-Z]{2}$"}
    assert fields["birthday"]["schema"]["kind"] == "temporal"
    assert fields["birthday"]["schema"]["format"] == "date"
    assert fields["addresses"]["schema"]["kind"] == "collection"
    assert fields["addresses"]["schema"]["element"]["kind"] == "dto"
    assert fields["byCity"]["schema"]["kind"] == "map"
    assert fields["byCity"]["schema"]["key"]["type"] == "java.lang.String"
    assert fields["byCity"]["schema"]["value"]["kind"] == "dto"
    assert fields["tags"]["schema"]["kind"] == "array"
    assert fields["tags"]["schema"]["element"]["type"] == "java.lang.String"
    assert fields["phone"]["constraints"]["format"] == "phone"
    assert fields["level"]["constraints"]["minimum"] == 1
    assert fields["level"]["constraints"]["maximum"] == 9
    assert fields["level"]["constraints"]["custom"]["Range"] == {
        "min": 1,
        "max": 9,
    }
    assert fields["grade"]["constraints"]["allowed_values"] == ["A", "B"]
    assert fields["grade"]["constraints"]["custom"]["EnumValue"]["values"] == [
        "A",
        "B",
    ]
    assert fields["ignored"].get("constraints") is None
    assert fields["biz"]["constraints"]["custom"] == {
        "BizCheck": {"code": "X"}
    }
    assert fields["five"]["constraints"]["min_length"] == 5
    assert fields["many"]["constraints"]["min_length"] == 12
    assert fields["lookahead"]["constraints"]["pattern"] == "^(?=a)a$"
    assert fields["external"]["schema"]["kind"] == "unknown"

    profiles, _ = build_profiles([], [], project_root=root)
    cases = generate_cases(profiles)
    case = next(
        item
        for item in cases
        if item.dubbo is not None and item.dubbo.method == "rich"
    )
    assert case.dubbo.arguments == [
        {
            "code": "samp",
            "quantity": 10,
            "email": "test@example.com",
            "region": "AA",
            "birthday": "2024-01-01",
            "addresses": [{"city": "sample"}],
            "byCity": {"sample": {"city": "sample"}},
            "tags": ["sample"],
            "phone": "13800138000",
            "level": 1,
            "grade": "A",
            "ignored": "sample",
            "biz": "sample",
            "five": ["sample"] * 5,
            "many": ["sample"] * 10,
            "lookahead": "sample",
            "external": {},
        }
    ]
    assert case.note is not None
    assert "BizCheck" in case.note
    assert "最多生成 10 个" in case.note
    assert "无法自动生成匹配值" in case.note
    assert "无法解析类型" in case.note
    # 可生成匹配值的 pattern（region）不再提示人工确认。
    assert "region 的 pattern" not in case.note


def test_parameter_annotations_become_constraints(tmp_path: Path) -> None:
    root = tmp_path
    package = root / "src" / "main" / "java" / "com" / "acme"
    package.mkdir(parents=True)
    (package / "ConstraintService.java").write_text(
        """package com.acme;

import java.time.LocalDate;

public interface ConstraintService {
    String echo(@Size(min = 2, max = 4) String code, @Positive int count, LocalDate date);
}
""",
        encoding="utf-8",
    )
    (package / "ConstraintServiceImpl.java").write_text(
        """package com.acme;

import org.apache.dubbo.config.annotation.DubboService;

@DubboService
public class ConstraintServiceImpl implements ConstraintService {
    public String echo(String code, int count, LocalDate date) { return null; }
}
""",
        encoding="utf-8",
    )

    result = _scan(root)
    provider = next(
        item for item in result.providers if item["method"] == "echo"
    )
    assert provider["parameter_schemas"][0]["constraints"] == {
        "min_length": 2,
        "max_length": 4,
    }
    assert provider["parameter_schemas"][1]["constraints"] == {
        "minimum": 0,
        "minimum_exclusive": True,
    }
    assert provider["parameter_schemas"][2]["kind"] == "temporal"

    profiles, _ = build_profiles([], [], project_root=root)
    case = next(
        item
        for item in generate_cases(profiles)
        if item.dubbo is not None and item.dubbo.method == "echo"
    )
    assert case.dubbo.arguments == ["samp", 1, "2024-01-01"]


def test_protocol_port_is_resolved_from_properties(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "OrderServiceImpl.java", IMPL)
    (root / "application.properties").write_text(
        "dubbo.protocol.port=20880\n", encoding="utf-8"
    )

    result = _scan(root)

    assert all(item["protocol_port"] == 20880 for item in result.providers)
    assert all(item["url_source"] == "protocol_port" for item in result.providers)

    profiles, _ = build_profiles([], [], project_root=root)
    cases = generate_cases(profiles)
    assert all(case.dubbo.target_port == 20880 for case in cases if case.dubbo)


def test_protocol_port_is_resolved_from_yaml(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "OrderServiceImpl.java", IMPL)
    (root / "application.yml").write_text(
        "dubbo:\n  protocol:\n    port: 20881\n", encoding="utf-8"
    )

    result = _scan(root)

    assert all(item["protocol_port"] == 20881 for item in result.providers)


def test_multiple_protocol_ports_are_ambiguous(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "OrderServiceImpl.java", IMPL)
    (root / "application.properties").write_text(
        "dubbo.protocol.port=20880\n", encoding="utf-8"
    )
    (root / "application.yml").write_text(
        "dubbo:\n  protocol:\n    port: 20881\n", encoding="utf-8"
    )

    result = _scan(root)

    assert all(item["protocol_port"] is None for item in result.providers)
    assert "dubbo_protocol_port_ambiguous" in result.warnings


def test_context_records_dubbo_providers_without_http_routes(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _write(root, "OrderServiceImpl.java", IMPL)
    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text("# API\n\n## 下单\n\n下单接口。\n", encoding="utf-8")

    prepared = prepare_context(
        root,
        ["docs/api.md"],
        scan_routes=False,
        out=tmp_path / "context",
    )
    context = prepared.context

    assert context["interface_evidence"]["dubbo"] == "present"
    assert "dubbo_providers_only" in context["warnings"]
    assert "routes_missing" not in context["warnings"]
    assert {
        item["endpoint"]
        for item in context["profiles"]
        if item["transport"] == "dubbo"
    } == {BY_ID_ENDPOINT, CREATE_ENDPOINT}
