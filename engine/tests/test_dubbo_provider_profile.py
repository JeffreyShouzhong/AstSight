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
