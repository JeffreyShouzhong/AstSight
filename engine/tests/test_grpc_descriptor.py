"""gRPC descriptor 编译、unary 索引与 stub 响应校验。"""
from __future__ import annotations

from pathlib import Path

import pytest

from astsight.mock import GrpcDescriptorError, GrpcDescriptorIndex

PROTO = """
syntax = "proto3";
package inventory.v1;

message ReserveRequest {
  string sku = 1;
}

message ReserveResponse {
  bool available = 1;
  int64 count = 2;
}

service InventoryService {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
  rpc Watch(ReserveRequest) returns (stream ReserveResponse);
}
"""


def _descriptor(tmp_path: Path) -> GrpcDescriptorIndex:
    proto = tmp_path / "inventory.proto"
    proto.write_text(PROTO, encoding="utf-8")
    return GrpcDescriptorIndex.load(proto)


def test_proto_compiles_and_indexes_unary_method(tmp_path: Path) -> None:
    index = _descriptor(tmp_path)

    method = index.method("/inventory.v1.InventoryService/Reserve")

    assert method.service == "inventory.v1.InventoryService"
    assert method.method == "Reserve"
    assert method.request_type == "inventory.v1.ReserveRequest"
    assert method.response_type == "inventory.v1.ReserveResponse"
    assert len(index.descriptor_sha256) == 64


def test_streaming_method_is_explicitly_unsupported(tmp_path: Path) -> None:
    index = _descriptor(tmp_path)

    with pytest.raises(GrpcDescriptorError) as error:
        index.method("/inventory.v1.InventoryService/Watch")
    assert error.value.code == "grpc_streaming_unsupported"


def test_response_body_is_validated_against_descriptor(tmp_path: Path) -> None:
    index = _descriptor(tmp_path)
    path = "/inventory.v1.InventoryService/Reserve"

    index.validate_response(path, {"available": True, "count": "1"})

    with pytest.raises(GrpcDescriptorError) as error:
        index.validate_response(path, {"available": True, "unknown": 1})
    assert error.value.code == "mock_stub_invalid"
