"""Triple/gRPC-compatible unary 动态 protobuf 调用。"""
from __future__ import annotations

import socket
from concurrent import futures
from pathlib import Path

import grpc
import pytest
from google.protobuf import message_factory

from astsight.mock.grpc_descriptor import GrpcDescriptorIndex
from astsight.sidecar import TripleUnaryCaller, is_grpc_compatible_target

PROTO = """
syntax = "proto3";
package inventory.v1;
message ReserveRequest { string sku = 1; }
message ReserveResponse {
  bool available = 1;
  string reservation_id = 2;
}
service InventoryService {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
"""
PATH = "/inventory.v1.InventoryService/Reserve"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def grpc_server(tmp_path: Path):
    proto = tmp_path / "inventory.proto"
    proto.write_text(PROTO, encoding="utf-8")
    index = GrpcDescriptorIndex.load(proto)
    method = index.method(PATH)
    request_cls = message_factory.GetMessageClass(
        index.pool.FindMessageTypeByName(method.request_type)
    )
    response_cls = message_factory.GetMessageClass(
        index.pool.FindMessageTypeByName(method.response_type)
    )

    def invoke(request, context):
        metadata = dict(context.invocation_metadata())
        if request.sku == "bad":
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad sku")
        return response_cls(
            available=request.sku == "SKU-1",
            reservation_id=f"{request.sku}:{metadata.get('dubbo-group', '')}",
        )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "inventory.v1.InventoryService",
                {
                    "Reserve": grpc.unary_unary_rpc_method_handler(
                        invoke,
                        request_deserializer=request_cls.FromString,
                        response_serializer=lambda response: (
                            response.SerializeToString()
                        ),
                    )
                },
            ),
        )
    )
    port = _free_port()
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    try:
        yield proto, port
    finally:
        server.stop(0).wait()


@pytest.mark.parametrize("scheme", ["tri", "grpc"])
def test_triple_unary_uses_descriptor_request_and_metadata(
    grpc_server,
    scheme: str,
) -> None:
    proto, port = grpc_server

    result = TripleUnaryCaller().invoke(
        interface="inventory.v1.InventoryService",
        method="Reserve",
        arguments=[{"sku": "SKU-1"}],
        url=f"{scheme}://127.0.0.1:{port}",
        descriptor_source=proto,
        grpc_path=PATH,
        group="inventory",
        version="1.0.0",
        serialization="protobuf",
        timeout_ms=5_000,
    )

    assert result.ok is True
    assert result.rpc_status == "OK"
    assert result.value == {
        "available": True,
        "reservation_id": "SKU-1:inventory",
    }


def test_triple_unary_maps_grpc_status_and_rejects_non_protobuf(
    grpc_server,
) -> None:
    proto, port = grpc_server
    caller = TripleUnaryCaller()
    url = f"tri://127.0.0.1:{port}"

    failed = caller.invoke(
        interface="inventory.v1.InventoryService",
        method="Reserve",
        arguments=[{"sku": "bad"}],
        url=url,
        descriptor_source=proto,
        serialization="protobuf",
    )

    assert failed.ok is False
    assert failed.rpc_status == "BAD_REQUEST"
    assert failed.error == "invalid_argument"
    assert failed.message == "bad sku"

    unsupported = caller.invoke(
        interface="inventory.v1.InventoryService",
        method="Reserve",
        arguments=[{}],
        url=url,
        descriptor_source=proto,
        serialization="hessian2",
    )
    assert unsupported.rpc_status == "UNSUPPORTED_PROTOCOL"


def test_triple_target_detection_is_scheme_based() -> None:
    assert is_grpc_compatible_target("tri://127.0.0.1:50051")
    assert is_grpc_compatible_target("grpc://127.0.0.1:50051")
    assert not is_grpc_compatible_target("dubbo://127.0.0.1:20880")
    assert not is_grpc_compatible_target(None)
