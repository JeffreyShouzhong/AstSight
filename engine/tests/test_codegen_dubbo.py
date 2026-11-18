"""Dubbo 用例的 pytest 生成：原生调用、断言与文件命名。"""
from __future__ import annotations

from pathlib import Path

from astsight.codegen import generate_pytest
from contracts import (
    Assertion,
    CaseStep,
    DubboCall,
    MqAssertion,
    TestCase,
    Transport,
)

ENDPOINT = (
    "dubbo:com.acme.OrderService#create(com.acme.CreateOrderRequest)"
    "?group=order&version=1.0.0"
)


def _case() -> TestCase:
    return TestCase(
        id="TC-0001",
        transport=Transport.DUBBO,
        endpoint=ENDPOINT,
        category="positive",
        method="create",
        path="",
        source=f"dubbo:{ENDPOINT}",
        asserts=[
            Assertion(kind="rpc_status", op="eq", expect="OK"),
            Assertion(kind="result_present", op="exists"),
            Assertion(
                kind="result_field", target="orderId", op="eq", expect="O-1"
            ),
        ],
        dubbo=DubboCall(
            interface="com.acme.OrderService",
            method="create",
            parameter_types=["com.acme.CreateOrderRequest"],
            arguments=[{"sku": "S-1"}],
            group="order",
            version="1.0.0",
            target_port=20880,
        ),
    )


def test_dubbo_case_generates_native_call_and_assertions(tmp_path: Path) -> None:
    result = generate_pytest([_case()], tmp_path)

    assert result.errors == []
    assert len(result.files) == 2
    test_file = next(
        path for path in result.files if path.name.startswith("test_dubbo_")
    )
    text = test_file.read_text(encoding="utf-8")
    assert "dubbo_call(" in text
    assert "interface='com.acme.OrderService'" in text
    assert "arguments=[{'sku': 'S-1'}]" in text
    assert "target_port=20880" in text
    assert "result.rpc_status == 'OK'" in text
    assert "result.value is not None" in text
    assert "dubbo_tools.field(result.value, 'orderId') == 'O-1'" in text
    assert "class TestDubboComAcmeOrderserviceCreate" in text

    conftest = (tmp_path / "conftest.py").read_text(encoding="utf-8")
    assert "def dubbo_call(" in conftest
    assert "def dubbo_client(" in conftest
    assert "ASTSIGHT_DUBBO_URL" in conftest
    compile(text, str(test_file), "exec")
    compile(conftest, str(tmp_path / "conftest.py"), "exec")


def test_multistep_dubbo_case_generates_refs(tmp_path: Path) -> None:
    by_id = (
        "dubbo:com.acme.OrderService#byId(com.acme.String)"
        "?group=order&version=1.0.0"
    )
    echo = (
        "dubbo:com.acme.OrderService#echo(java.lang.String)"
        "?group=order&version=1.0.0"
    )
    case = TestCase(
        id="TC-0002",
        transport=Transport.DUBBO,
        endpoint=echo,
        category="business",
        method="echo",
        path="",
        source="dubbo:flow",
        asserts=[],
        steps=[
            CaseStep(
                name="first",
                transport=Transport.DUBBO,
                endpoint=by_id,
                method="byId",
                path="",
                dubbo=DubboCall(
                    interface="com.acme.OrderService",
                    method="byId",
                    parameter_types=["java.lang.String"],
                    arguments=["O-1"],
                    group="order",
                    version="1.0.0",
                ),
                asserts=[
                    Assertion(kind="rpc_status", op="eq", expect="OK"),
                    Assertion(
                        kind="result_field", target="orderId", op="exists"
                    ),
                ],
            ),
            CaseStep(
                name="second",
                transport=Transport.DUBBO,
                endpoint=echo,
                method="echo",
                path="",
                dubbo=DubboCall(
                    interface="com.acme.OrderService",
                    method="echo",
                    parameter_types=["java.lang.String"],
                    arguments=[{"$ref": "first.orderId"}],
                    group="order",
                    version="1.0.0",
                ),
                asserts=[
                    Assertion(kind="rpc_status", op="eq", expect="OK"),
                    Assertion(kind="result_present", op="exists"),
                ],
            ),
        ],
    )

    result = generate_pytest([case], tmp_path)
    assert result.errors == []
    test_file = next(
        path for path in result.files if path.name.startswith("test_dubbo_")
    )
    text = test_file.read_text(encoding="utf-8")
    assert "result_first = dubbo_call(" in text
    assert "body_first = result_first.value" in text
    assert "result_second = dubbo_call(" in text
    assert "arguments=step_tools.refs(" in text
    assert "{'first': body_first}" in text
    assert "body_second = result_second.value" in text
    assert "dubbo_tools.field(result_first.value, 'orderId')" in text
    assert "def test_tc_0002_business(self" in text
    compile(text, str(test_file), "exec")


def test_conftest_reads_service_target_map(tmp_path: Path) -> None:
    result = generate_pytest([_case()], tmp_path)
    conftest = (tmp_path / "conftest.py").read_text(encoding="utf-8")
    assert "ASTSIGHT_DUBBO_TARGETS" in conftest
    assert "_dubbo_service_key" in conftest
    assert "dubbo_target_unresolved" in conftest
    assert "DUBBO_TARGET_ERROR.split" in conftest


def test_dubbo_error_assertions_are_generated(tmp_path: Path) -> None:
    case = _case().model_copy(
        update={
            "asserts": [
                Assertion(kind="rpc_status", op="in", expect=["TIMEOUT", "NETWORK"]),
                Assertion(kind="error_code", op="eq", expect="rpc_timeout"),
                Assertion(kind="error_message", op="contains", expect="timeout"),
            ]
        }
    )
    result = generate_pytest([case], tmp_path)
    text = next(
        path for path in result.files if path.name.startswith("test_dubbo_")
    ).read_text(encoding="utf-8")

    assert "result.rpc_status in ['TIMEOUT', 'NETWORK']" in text
    assert "result.error == 'rpc_timeout'" in text
    assert "'timeout' in str(result.message or '')" in text


def test_triple_case_generates_descriptor_and_grpc_compatible_dispatch(
    tmp_path: Path,
) -> None:
    endpoint = (
        "dubbo:inventory.v1.InventoryService#Reserve"
        "(inventory.v1.ReserveRequest)"
    )
    case = TestCase(
        id="TC-TRIPLE",
        transport=Transport.DUBBO,
        endpoint=endpoint,
        category="positive",
        method="Reserve",
        path="",
        source="dubbo:triple",
        asserts=[
            Assertion(kind="rpc_status", op="eq", expect="OK"),
            Assertion(
                kind="result_field",
                target="reservation_id",
                op="exists",
            ),
        ],
        dubbo=DubboCall(
            interface="inventory.v1.InventoryService",
            method="Reserve",
            parameter_types=["inventory.v1.ReserveRequest"],
            arguments=[{"sku": "SKU-1"}],
            url="tri://127.0.0.1:50053",
            serialization="protobuf",
            descriptor_source="src/main/proto/inventory.proto",
            grpc_path="/inventory.v1.InventoryService/Reserve",
            compatibility="grpc_compatible",
        ),
    )

    result = generate_pytest([case], tmp_path, project_root=tmp_path)

    assert result.errors == []
    text = next(
        path for path in result.files if path.name.startswith("test_dubbo_")
    ).read_text(encoding="utf-8")
    assert "url='tri://127.0.0.1:50053'" in text
    assert "serialization='protobuf'" in text
    assert (
        f"descriptor_source={str((tmp_path / 'src/main/proto/inventory.proto').resolve())!r}"
        in text
    )
    assert "grpc_path='/inventory.v1.InventoryService/Reserve'" in text
    assert "compatibility='grpc_compatible'" in text
    conftest = (tmp_path / "conftest.py").read_text(encoding="utf-8")
    assert 'scheme in {"tri", "grpc"}' in conftest
    assert "from astsight.sidecar.triple import invoke_triple" in conftest
    compile(text, str(tmp_path / "test_triple.py"), "exec")
    compile(conftest, str(tmp_path / "conftest.py"), "exec")


def test_case_data_refs_become_literals_and_mock_reply(tmp_path: Path) -> None:
    base = _case()
    dep_id = "dubbo:com.acme.ProductService#get(java.lang.String)"
    case = base.model_copy(
        update={
            "data": {"product": {"id": "P-1001", "category": "phone"}},
            "dubbo": base.dubbo.model_copy(
                update={"arguments": [{"$data": "product.id"}]}
            ),
            "mock_bindings": {dep_id: "success"},
            "mock_replies": {
                dep_id: {
                    "id": {"$data": "product.id"},
                    "category": {"$data": "product.category"},
                }
            },
        }
    )
    result = generate_pytest([case], tmp_path)

    assert result.errors == []
    text = next(
        path for path in result.files if path.name.startswith("test_dubbo_")
    ).read_text(encoding="utf-8")
    assert "arguments=['P-1001']" in text
    assert "'reply': {'id': 'P-1001', 'category': 'phone'}" in text
    assert "$data" not in text
    compile(text, str(tmp_path / "test_data.py"), "exec")


def test_unresolved_case_data_ref_fails_codegen(tmp_path: Path) -> None:
    base = _case()
    case = base.model_copy(
        update={
            "data": {"product": {"id": "P-1001"}},
            "dubbo": base.dubbo.model_copy(
                update={"arguments": [{"$data": "product.sku"}]}
            ),
        }
    )
    result = generate_pytest([case], tmp_path)
    assert any(
        "data_ref_unresolved" in item.get("error", "")
        for item in result.errors
    )
    assert not any(
        path.name.startswith("test_dubbo_") for path in result.files
    )


def test_mq_assertion_generates_producer_message_check(tmp_path: Path) -> None:
    case = _case().model_copy(
        update={
            "mq_assertions": [
                MqAssertion(
                    dep_id="mq:kafka:orders-topic",
                    topic="orders-topic",
                    key="O-1",
                    headers={"trace": "t1"},
                    body={"orderId": "O-1"},
                    count=1,
                )
            ]
        }
    )
    result = generate_pytest([case], tmp_path)
    assert result.errors == []
    text = next(
        path for path in result.files if path.name.startswith("test_dubbo_")
    ).read_text(encoding="utf-8")
    assert "def test_tc_0001_positive(self" in text
    assert "mq_tools" in text
    assert "mq_tools.expect(topic='orders-topic'" in text
    assert "dep_id='mq:kafka:orders-topic'" in text
    assert "count=1" in text
    conftest = (tmp_path / "conftest.py").read_text(encoding="utf-8")
    assert "class _MqTools" in conftest
    assert "/_admin/mq/messages" in conftest
    compile(text, str(tmp_path / "test_mq.py"), "exec")
    compile(conftest, str(tmp_path / "conftest.py"), "exec")
