"""Java 下游契约提取：响应类型、依赖 jar、DTO 字段与 Feign 接口。"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from astsight.mock.semantic import build_mock_semantic
from astsight.profile.downstream import DownstreamScanner
from astsight.profile.javaclass import descriptor_to_type, parse_class
from astsight.profile.javadeps import (
    Dependency,
    discover_jars,
    locate_jar,
    parse_gradle,
    parse_pom,
    project_dependencies,
)
from astsight.profile.javatypes import ContractIndex, scan_feign_interfaces
from astsight.profile.langscan import scan_language_file


def _write(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


# ───────────────────────── class 文件构造 ─────────────────────────


def _u2(value: int) -> bytes:
    return struct.pack(">H", value)


def _utf8(value: str) -> bytes:
    raw = value.encode("utf-8")
    return b"\x01" + _u2(len(raw)) + raw


def _class_bytes(
    *,
    name: str = "com/example/Item",
    fields: list[str] | None = None,
    signature: str = "",
    annotation: str = "",
    annotation_value: str = "",
) -> bytes:
    """拼一个只含常量池、字段和类注解的最小 class 文件。"""
    fields = fields or []
    pool: list[bytes] = []

    def add(raw: bytes) -> int:
        pool.append(raw)
        return len(pool)

    name_index = add(_utf8(name))
    this_class = add(b"\x07" + _u2(name_index))
    super_name = add(_utf8("java/lang/Object"))
    super_class = add(b"\x07" + _u2(super_name))

    signature_name = add(_utf8("Signature")) if signature else 0
    signature_value = add(_utf8(signature)) if signature else 0

    field_indexes: list[tuple[int, int, str]] = []
    for item in fields:
        field_name, descriptor = item.split(":", 1)
        field_indexes.append(
            (add(_utf8(field_name)), add(_utf8(descriptor)), descriptor)
        )

    def field_attrs(descriptor: str) -> list[bytes]:
        # 只有引用类型字段才带泛型签名；基本类型不适用。
        if not signature or descriptor.startswith(("I", "J", "Z", "D", "F", "S", "B", "C")):
            return []
        return [_u2(signature_name) + struct.pack(">I", 2) + _u2(signature_value)]
    class_attrs: list[bytes] = []
    if annotation:
        annotation_name = add(_utf8("RuntimeVisibleAnnotations"))
        type_index = add(_utf8(f"L{annotation};"))
        member = add(_utf8("name"))
        value = add(_utf8(annotation_value))
        payload = (
            _u2(1)
            + _u2(type_index)
            + _u2(1)
            + _u2(member)
            + b"s"
            + _u2(value)
        )
        class_attrs.append(
            _u2(annotation_name)
            + struct.pack(">I", len(payload))
            + payload
        )

    out = b"\xca\xfe\xba\xbe" + _u2(0) + _u2(52)
    out += _u2(len(pool) + 1) + b"".join(pool)
    out += _u2(0x0021) + _u2(this_class) + _u2(super_class)
    out += _u2(0)  # interfaces
    out += _u2(len(field_indexes))
    for field_name_index, descriptor_index, descriptor in field_indexes:
        out += _u2(0x0002) + _u2(field_name_index) + _u2(descriptor_index)
        attrs = field_attrs(descriptor)
        out += _u2(len(attrs)) + b"".join(attrs)
    out += _u2(0)  # methods
    out += _u2(len(class_attrs)) + b"".join(class_attrs)
    return out


def _write_jar(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


# ───────────────────────── 调用点响应类型 ─────────────────────────


PAY_CONTROLLER = """
package com.example.pay;

@RestController
@RequestMapping("/pay")
public class PayController {
    @Value("${pay.base-url}")
    private String baseUrl;

    @GetMapping("/detail")
    public PayResult detail() {
        return restTemplate.getForObject(baseUrl + "/detail", PayResult.class);
    }
}
"""


def _scan(tmp_path: Path, source: str, handler: str):
    path = _write(tmp_path, "src/main/java/com/example/pay/PayController.java", source)
    return scan_language_file(
        path=path,
        rel="src/main/java/com/example/pay/PayController.java",
        language="java",
        handler=handler,
    )


def test_resttemplate_response_type_from_class_literal(tmp_path: Path) -> None:
    scan = _scan(tmp_path, PAY_CONTROLLER, "PayController.detail")
    assert len(scan.sites) == 1
    assert scan.sites[0].response_type == "PayResult"


def test_resttemplate_parameterized_type_reference(tmp_path: Path) -> None:
    source = """
package com.example.pay;

@RestController
public class PayController {
    @GetMapping("/items")
    public List<PayItem> items() {
        ResponseEntity<List<PayItem>> response = restTemplate.exchange(
            baseUrl + "/items",
            HttpMethod.GET,
            null,
            new ParameterizedTypeReference<List<PayItem>>() {}
        );
        return response.getBody();
    }
}
"""
    scan = _scan(tmp_path, source, "PayController.items")
    assert [site.response_type for site in scan.sites] == ["List<PayItem>"]


def test_resttemplate_variable_is_not_a_response_type(tmp_path: Path) -> None:
    source = """
package com.example.pay;

@RestController
public class PayController {
    @GetMapping("/detail")
    public Object detail(Class<?> clazz) {
        return restTemplate.getForObject(baseUrl + "/detail", clazz);
    }
}
"""
    scan = _scan(tmp_path, source, "PayController.detail")
    assert scan.sites[0].response_type is None


def test_webclient_body_to_mono_response_type(tmp_path: Path) -> None:
    source = """
package com.example.pay;

@RestController
public class PricingClient {
    @GetMapping("/price")
    public Mono<Price> price() {
        return client.get()
            .uri(baseUrl + "/price")
            .retrieve()
            .bodyToMono(Price.class);
    }
}
"""
    scan = _scan(tmp_path, source, "PricingClient.price")
    assert len(scan.sites) == 1
    assert scan.sites[0].client == "webclient"
    assert scan.sites[0].response_type == "Price"


# ───────────────────────── class 文件与类型 ─────────────────────────


def test_descriptor_to_type_uses_generic_signature() -> None:
    assert (
        descriptor_to_type(
            "Ljava/util/List;", "Ljava/util/List<Lcom/example/Item;>;"
        )
        == "java.util.List<com.example.Item>"
    )
    assert descriptor_to_type("[Ljava/lang/String;", "") == "java.lang.String[]"
    assert descriptor_to_type("I", "") == "int"


def test_descriptor_to_type_keeps_nested_arguments() -> None:
    signature = (
        "Ljava/util/Map<Ljava/lang/String;"
        "Ljava/util/function/Supplier<Lcom/example/Item;>;>;"
    )
    assert descriptor_to_type("Ljava/util/Map;", signature) == (
        "java.util.Map<java.lang.String, "
        "java.util.function.Supplier<com.example.Item>>"
    )


def test_parse_class_reads_fields_and_annotations() -> None:
    data = _class_bytes(
        fields=["id:J", "items:Ljava/util/List;"],
        signature="Ljava/util/List<Lcom/example/Item;>;",
        annotation="org/springframework/cloud/openfeign/FeignClient",
        annotation_value="item-api",
    )
    info = parse_class(data)
    assert info.name == "com.example.Item"
    assert [(item.name, item.type_name()) for item in info.fields] == [
        ("id", "long"),
        ("items", "java.util.List<com.example.Item>"),
    ]
    assert info.fields[0].is_static is False
    assert info.annotations["FeignClient"]["name"] == "item-api"


def test_parse_class_rejects_non_class_bytes() -> None:
    import pytest

    from astsight.profile.javaclass import ClassFileError

    with pytest.raises(ClassFileError):
        parse_class(b"not a class")


# ───────────────────────── 构建文件 ─────────────────────────


def test_parse_pom_resolves_properties(tmp_path: Path) -> None:
    pom = _write(
        tmp_path,
        "pom.xml",
        """<project>
  <parent>
    <groupId>com.example</groupId>
    <artifactId>parent</artifactId>
    <version>3.1.0</version>
  </parent>
  <properties>
    <item.version>2.0.1</item.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>item-api</artifactId>
      <version>${item.version}</version>
    </dependency>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>item-api</artifactId>
      <version>${item.version}</version>
    </dependency>
  </dependencies>
</project>
""",
    )
    deps = parse_pom(pom)
    assert [dep.coordinate for dep in deps] == ["com.example:item-api:2.0.1"]


def test_parse_gradle_reads_coordinates(tmp_path: Path) -> None:
    gradle = _write(
        tmp_path,
        "build.gradle",
        """
dependencies {
    implementation 'com.example:item-api:1.4.0'
    api("com.example:common-model:2.0.0")
    testImplementation 'org.junit:junit:5.0.0'
}
""",
    )
    deps = parse_gradle(gradle)
    assert [dep.coordinate for dep in deps] == [
        "com.example:item-api:1.4.0",
        "com.example:common-model:2.0.0",
    ]


def test_locate_jar_in_local_maven_repository(tmp_path: Path) -> None:
    m2 = tmp_path / "repo"
    jar = _write_jar(
        m2 / "com" / "example" / "item-api" / "1.0.0" / "item-api-1.0.0.jar",
        {"com/example/Item.class": _class_bytes()},
    )
    found = locate_jar(
        Dependency(group="com.example", artifact="item-api", version="1.0.0"),
        project_root=tmp_path,
        m2_root=m2,
    )
    assert found == jar


def test_discover_jars_prefers_contract_artifacts(tmp_path: Path) -> None:
    m2 = tmp_path / "repo"
    _write_jar(
        m2 / "com" / "example" / "utils" / "1.0.0" / "utils-1.0.0.jar",
        {"com/example/Util.class": _class_bytes(name="com/example/Util")},
    )
    api = _write_jar(
        m2 / "com" / "example" / "item-api" / "1.0.0" / "item-api-1.0.0.jar",
        {"com/example/Item.class": _class_bytes()},
    )
    _write(
        tmp_path,
        "pom.xml",
        """<project>
  <dependencies>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>utils</artifactId>
      <version>1.0.0</version>
    </dependency>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>item-api</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
</project>
""",
    )
    jars = discover_jars(tmp_path, m2_root=m2)
    assert jars[0] == api
    assert project_dependencies(tmp_path)[0].artifact == "utils"


# ───────────────────────── 契约解析 ─────────────────────────


def test_contract_index_resolves_source_record(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/example/item/Item.java",
        """
package com.example.item;

public record Item(Long id, String name, List<String> tags) {}
""",
    )
    controller = _write(
        tmp_path,
        "src/main/java/com/example/item/ItemController.java",
        """
package com.example.item;

import java.util.List;

public class ItemController {
    public List<Item> items() {
        return restTemplate.exchange(url, HttpMethod.GET, null,
            new ParameterizedTypeReference<List<Item>>() {}).getBody();
    }
}
""",
    )
    contract = ContractIndex(tmp_path).resolve("List<Item>", context_file=controller)
    assert contract is not None
    assert contract.name == "Item"
    assert contract.kind == "source"
    assert contract.container == "list"
    assert [(item.name, item.type) for item in contract.fields] == [
        ("id", "Long"),
        ("name", "String"),
        ("tags", "List<String>"),
    ]


def test_contract_index_resolves_type_from_dependency_jar(tmp_path: Path) -> None:
    m2 = tmp_path / "repo"
    _write_jar(
        m2 / "com" / "example" / "item-api" / "1.0.0" / "item-api-1.0.0.jar",
        {
            "com/example/item/Item.class": _class_bytes(
                name="com/example/item/Item",
                fields=["code:I", "message:Ljava/lang/String;"],
            )
        },
    )
    _write(
        tmp_path,
        "pom.xml",
        """<project>
  <dependencies>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>item-api</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
</project>
""",
    )
    contract = ContractIndex(tmp_path, m2_root=m2).resolve("com.example.item.Item")
    assert contract is not None
    assert contract.kind == "jar"
    assert [(item.name, item.type) for item in contract.fields] == [
        ("code", "int"),
        ("message", "java.lang.String"),
    ]


def test_contract_index_does_not_guess_unknown_types(tmp_path: Path) -> None:
    assert ContractIndex(tmp_path).resolve("MissingResult") is None


def test_scan_feign_interfaces_from_source(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/example/item/ItemClient.java",
        """
package com.example.item;

@FeignClient(name = "item-api", path = "/items")
public interface ItemClient {
    @GetMapping("/{id}")
    Item getItem(@PathVariable("id") Long id);

    @PostMapping
    Item create(@RequestBody Item item);
}
""",
    )
    interfaces = scan_feign_interfaces(tmp_path)
    assert len(interfaces) == 1
    client = interfaces[0]
    assert client.fqn == "com.example.item.ItemClient"
    assert client.service == "item-api"
    assert [
        (item.name, item.http_method, item.path) for item in client.methods
    ] == [
        ("getItem", "GET", "/{id}"),
        ("create", "POST", ""),
    ]


def test_scan_feign_interfaces_from_dependency_jar(tmp_path: Path) -> None:
    m2 = tmp_path / "repo"
    _write_jar(
        m2 / "com" / "example" / "item-api" / "1.0.0" / "item-api-1.0.0.jar",
        {
            "com/example/item/ItemClient.class": _class_bytes(
                name="com/example/item/ItemClient",
                annotation="org/springframework/cloud/openfeign/FeignClient",
                annotation_value="item-api",
            )
        },
    )
    _write(
        tmp_path,
        "pom.xml",
        """<project>
  <dependencies>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>item-api</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
</project>
""",
    )
    interfaces = scan_feign_interfaces(
        tmp_path, index=ContractIndex(tmp_path, m2_root=m2)
    )
    assert [item.fqn for item in interfaces] == ["com.example.item.ItemClient"]
    assert interfaces[0].service == "item-api"


# ───────────────────────── 扫描与 Mock 语义 ─────────────────────────


def test_downstream_scan_attaches_response_contract(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/example/pay/PayResult.java",
        """
package com.example.pay;

public class PayResult {
    private int code;
    private String message;
    private boolean success;
}
""",
    )
    _write(
        tmp_path,
        "src/main/java/com/example/pay/PayController.java",
        PAY_CONTROLLER,
    )
    scan = DownstreamScanner(tmp_path).scan(
        "PayController.detail",
        file_hint="src/main/java/com/example/pay/PayController.java",
    )
    assert len(scan.deps) == 1
    dep = scan.deps[0]
    assert dep["response_types"] == ["PayResult"]
    contract = dep["response_contract"]
    assert contract["status"] == "resolved"
    assert contract["types"][0]["fqn"] == "com.example.pay.PayResult"
    assert [field["name"] for field in contract["types"][0]["fields"]] == [
        "code",
        "message",
        "success",
    ]


FEIGN_ITEM_CLIENT = """
package com.example.item;

@FeignClient(name = "item-api", path = "/items")
public interface ItemClient {
    @GetMapping("/{id}")
    Item getItem(@PathVariable("id") Long id);
}
"""

FEIGN_ITEM_DTO = """
package com.example.item;

public class Item {
    private Long id;
    private String name;
}
"""

FEIGN_CONTROLLER = """
package com.example.item;

@RestController
@RequestMapping("/orders")
public class OrderController {
    private final ItemClient itemClient;

    public OrderController(ItemClient itemClient) {
        this.itemClient = itemClient;
    }

    @GetMapping("/{id}")
    public Item detail(@PathVariable("id") Long id) {
        return itemClient.getItem(id);
    }
}
"""


def _write_feign_project(tmp_path: Path, client: str = FEIGN_ITEM_CLIENT) -> Path:
    _write(tmp_path, "src/main/java/com/example/item/ItemClient.java", client)
    _write(tmp_path, "src/main/java/com/example/item/Item.java", FEIGN_ITEM_DTO)
    _write(
        tmp_path,
        "src/main/java/com/example/item/OrderController.java",
        FEIGN_CONTROLLER,
    )
    return tmp_path


def test_feign_call_site_uses_service_identity(tmp_path: Path) -> None:
    _write_feign_project(tmp_path)
    scan = DownstreamScanner(tmp_path).scan(
        "OrderController.detail",
        file_hint="src/main/java/com/example/item/OrderController.java",
    )
    assert scan.checked is True
    assert len(scan.deps) == 1
    dep = scan.deps[0]
    assert dep["dep_id"] == "http:svc:item-api GET /items/{}"
    assert dep["evidence"] == "svc"
    assert dep["route_host"] == "item-api"
    assert dep["service_name"] == "item-api"
    assert dep["target"] == "http://item-api/items/{}"
    assert dep["response_types"] == ["Item"]
    assert dep["response_contract"]["status"] == "resolved"
    assert [field["name"] for field in dep["response_contract"]["types"][0]["fields"]] == [
        "id",
        "name",
    ]


def test_feign_request_mapping_reads_static_request_method(
    tmp_path: Path,
) -> None:
    client = """
package com.example.item;

@FeignClient("ItemApi")
public interface ItemClient {
    @RequestMapping(value = "/items/{id}", method = GET)
    Item getItem(@PathVariable("id") Long id);
}
"""
    _write_feign_project(tmp_path, client)
    interfaces = scan_feign_interfaces(tmp_path)
    methods = interfaces[0].methods
    assert [(item.http_method, item.path) for item in methods] == [
        ("GET", "/items/{id}")
    ]

    scan = DownstreamScanner(tmp_path).scan(
        "OrderController.detail",
        file_hint="src/main/java/com/example/item/OrderController.java",
    )
    dep = scan.deps[0]
    assert dep["dep_id"] == "http:svc:itemapi GET /items/{}"
    assert dep["route_host"] == "itemapi"
    # 服务发现按注册名精确匹配，dep_id 用小写，但服务名保留原始大小写。
    assert dep["service_name"] == "ItemApi"


def test_feign_call_site_uses_explicit_url(tmp_path: Path) -> None:
    client = """
package com.example.item;

@FeignClient(name = "item-api", url = "http://items.local", path = "/items")
public interface ItemClient {
    @GetMapping("/{id}")
    Item getItem(@PathVariable("id") Long id);
}
"""
    _write_feign_project(tmp_path, client)
    scan = DownstreamScanner(tmp_path).scan(
        "OrderController.detail",
        file_hint="src/main/java/com/example/item/OrderController.java",
    )
    dep = scan.deps[0]
    assert dep["dep_id"] == "http:svc:item-api GET /items/{}"
    assert dep["target"] == "http://items.local/items/{}"


def test_feign_interface_without_call_is_not_a_dep(tmp_path: Path) -> None:
    _write_feign_project(tmp_path)
    _write(
        tmp_path,
        "src/main/java/com/example/item/OrderController.java",
        """
package com.example.item;

@RestController
public class OrderController {
    private final ItemClient itemClient;

    @GetMapping("/ping")
    public String ping() {
        return "pong";
    }
}
""",
    )
    scan = DownstreamScanner(tmp_path).scan(
        "OrderController.ping",
        file_hint="src/main/java/com/example/item/OrderController.java",
    )
    assert scan.deps == []


def test_downstream_scan_marks_unresolved_contract(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/example/pay/PayController.java",
        PAY_CONTROLLER,
    )
    scan = DownstreamScanner(tmp_path).scan(
        "PayController.detail",
        file_hint="src/main/java/com/example/pay/PayController.java",
    )
    contract = scan.deps[0]["response_contract"]
    assert contract["status"] == "missing"
    assert contract["unresolved"] == ["PayResult"]


def test_mock_semantic_builds_success_body_from_contract() -> None:
    context = {
        "context_id": "ctx",
        "profiles": [
            {
                "endpoint": "GET /pay/detail",
                "downstream_deps": [
                    {
                        "dep_id": "http:call:pay:GET /detail",
                        "method": "GET",
                        "path_key": "/detail",
                        "response_contract": {
                            "status": "resolved",
                            "types": [
                                {
                                    "name": "PayResult",
                                    "fqn": "com.example.pay.PayResult",
                                    "container": None,
                                    "fields": [
                                        {"name": "code", "type": "int"},
                                        {"name": "message", "type": "String"},
                                        {"name": "success", "type": "boolean"},
                                        {"name": "items", "type": "List<Item>"},
                                    ],
                                }
                            ],
                            "unresolved": [],
                        },
                    }
                ],
            }
        ],
    }
    semantic = build_mock_semantic(context)
    branch = semantic.deps[0].branches[0]
    assert branch.branch == "success"
    assert branch.stub.status == 200
    assert branch.stub.body == {
        "code": 0,
        "message": "",
        "success": False,
        "items": [],
    }
    assert "A 的响应类型" in branch.semantics


def test_mock_semantic_without_contract_keeps_empty_body() -> None:
    context = {
        "context_id": "ctx",
        "profiles": [
            {
                "endpoint": "GET /pay/detail",
                "downstream_deps": [
                    {
                        "dep_id": "http:call:pay:GET /detail",
                        "method": "GET",
                        "path_key": "/detail",
                    }
                ],
            }
        ],
    }
    semantic = build_mock_semantic(context)
    assert semantic.deps[0].branches[0].stub.body == {}
