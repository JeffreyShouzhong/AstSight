"""gRPC 下游 Mock 的 descriptor 编译、索引与响应校验。"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

MAX_PROTO_BYTES = 16 * 1024 * 1024


class GrpcDescriptorError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class GrpcMethod:
    grpc_path: str
    service: str
    method: str
    request_type: str
    response_type: str
    client_streaming: bool = False
    server_streaming: bool = False


def _descriptor_bytes_from_proto(path: Path, include_dirs: Iterable[Path]) -> bytes:
    try:
        from grpc_tools import protoc
        import grpc_tools
    except ImportError as exc:
        raise GrpcDescriptorError(
            "proto_compiler_unavailable",
            "需要 grpcio-tools 才能编译 .proto",
        ) from exc

    includes = [path.parent, *include_dirs]
    bootstrap = Path(grpc_tools.__file__).resolve().parent / "_proto"
    if bootstrap.is_dir():
        includes.append(bootstrap)
    with tempfile.TemporaryDirectory(prefix="astsight-protoc-") as tmp:
        output = Path(tmp) / "descriptor.pb"
        args = ["protoc"]
        args.extend(f"-I{item.resolve()}" for item in includes)
        args.extend(
            [
                "--include_imports",
                f"--descriptor_set_out={output}",
                str(path),
            ]
        )
        code = protoc.main(args)
        if code != 0 or not output.is_file():
            raise GrpcDescriptorError(
                "proto_compile_failed",
                f"protoc exit={code} source={path}",
            )
        return output.read_bytes()


def _descriptor_bytes(
    source: str | Path, include_dirs: Iterable[str | Path] = ()
) -> tuple[bytes, str]:
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise GrpcDescriptorError("mock_descriptor_missing", str(path))
    if path.stat().st_size > MAX_PROTO_BYTES:
        raise GrpcDescriptorError(
            "proto_descriptor_invalid",
            f"{path}: file too large",
        )
    includes = [Path(item).expanduser().resolve() for item in include_dirs]
    if path.suffix == ".protoset":
        return path.read_bytes(), str(path)
    if path.suffix == ".proto":
        return _descriptor_bytes_from_proto(path, includes), str(path)
    raise GrpcDescriptorError(
        "proto_descriptor_invalid",
        f"unsupported descriptor source: {path.suffix}",
    )


class GrpcDescriptorIndex:
    """一个 FileDescriptorSet 的只读索引。"""

    def __init__(self, payload: bytes, *, source: str):
        if not payload:
            raise GrpcDescriptorError("proto_descriptor_invalid", "empty descriptor")
        self.source = source
        self.descriptor_sha256 = hashlib.sha256(payload).hexdigest()
        descriptor_set = descriptor_pb2.FileDescriptorSet()
        try:
            descriptor_set.ParseFromString(payload)
        except Exception as exc:
            raise GrpcDescriptorError(
                "proto_descriptor_invalid",
                f"FileDescriptorSet parse failed: {exc}",
            ) from exc
        if not descriptor_set.file:
            raise GrpcDescriptorError("proto_descriptor_invalid", "no proto files")
        self._files = list(descriptor_set.file)

        self.pool = descriptor_pool.DescriptorPool()
        pending = list(descriptor_set.file)
        failures: dict[str, str] = {}
        while pending:
            remaining = []
            for file_proto in pending:
                try:
                    self.pool.AddSerializedFile(file_proto.SerializeToString())
                except Exception as exc:
                    remaining.append(file_proto)
                    failures[file_proto.name] = str(exc)
            if len(remaining) == len(pending):
                raise GrpcDescriptorError(
                    "proto_descriptor_invalid",
                    f"cannot resolve imports: {failures}",
                )
            pending = remaining

    @classmethod
    def load(
        cls,
        source: str | Path,
        *,
        include_dirs: Iterable[str | Path] = (),
    ) -> "GrpcDescriptorIndex":
        payload, resolved = _descriptor_bytes(source, include_dirs)
        return cls(payload, source=resolved)

    def method(self, grpc_path: str) -> GrpcMethod:
        value = str(grpc_path or "").strip()
        if not value.startswith("/") or value.count("/") != 2:
            raise GrpcDescriptorError("grpc_path_invalid", value)
        service_name, method_name = value[1:].split("/", 1)
        try:
            service = self.pool.FindServiceByName(service_name)
        except KeyError as exc:
            raise GrpcDescriptorError(
                "grpc_method_not_found",
                f"service={service_name}",
            ) from exc
        method = service.methods_by_name.get(method_name)
        if method is None:
            raise GrpcDescriptorError(
                "grpc_method_not_found",
                value,
            )
        if method.client_streaming or method.server_streaming:
            raise GrpcDescriptorError("grpc_streaming_unsupported", value)
        return GrpcMethod(
            grpc_path=value,
            service=service_name,
            method=method_name,
            request_type=method.input_type.full_name,
            response_type=method.output_type.full_name,
        )

    def all_methods(self) -> list[GrpcMethod]:
        methods: list[GrpcMethod] = []
        for file_proto in self._files:
            package = file_proto.package.strip(".")
            for service in file_proto.service:
                full_service = (
                    f"{package}.{service.name}" if package else service.name
                )
                for method in service.method:
                    methods.append(
                        GrpcMethod(
                            grpc_path=f"/{full_service}/{method.name}",
                            service=full_service,
                            method=method.name,
                            request_type=method.input_type.lstrip("."),
                            response_type=method.output_type.lstrip("."),
                            client_streaming=bool(method.client_streaming),
                            server_streaming=bool(method.server_streaming),
                        )
                    )
        return sorted(methods, key=lambda item: item.grpc_path)

    def unary_methods(self) -> list[GrpcMethod]:
        return [
            item for item in self.all_methods()
            if not item.client_streaming and not item.server_streaming
        ]

    def validate_response(self, grpc_path: str, body: Any) -> None:
        method = self.method(grpc_path)
        try:
            message_type = self.pool.FindMessageTypeByName(method.response_type)
            message = message_factory.GetMessageClass(message_type)()
            json_format.ParseDict(
                {} if body is None else body,
                message,
                ignore_unknown_fields=False,
            )
        except Exception as exc:
            raise GrpcDescriptorError(
                "mock_stub_invalid",
                f"{grpc_path}: {type(exc).__name__}: {exc}",
            ) from exc

    def sample_message(
        self,
        type_name: str,
        *,
        depth: int = 4,
        _seen: set[str] | None = None,
    ) -> Any:
        """按 descriptor 生成可解析的 protobuf JSON 默认值。"""
        if depth <= 0:
            return {}
        seen = set(_seen or ())
        if type_name in seen:
            return {}
        seen.add(type_name)
        try:
            message_type = self.pool.FindMessageTypeByName(type_name)
        except KeyError:
            return {}
        if type_name == "google.protobuf.Timestamp":
            return "1970-01-01T00:00:00Z"
        if type_name == "google.protobuf.Duration":
            return "0s"
        if type_name == "google.protobuf.Any":
            return {}
        result: dict[str, Any] = {}
        oneofs: set[str] = set()
        for field in message_type.fields:
            if field.containing_oneof is not None:
                oneof_name = field.containing_oneof.name
                if oneof_name in oneofs:
                    continue
                oneofs.add(oneof_name)
            if (
                field.message_type is not None
                and field.message_type.GetOptions().map_entry
            ):
                result[field.json_name] = {}
                continue
            if field.is_repeated:
                result[field.json_name] = []
                continue
            result[field.json_name] = self._sample_field(
                field,
                depth=depth,
                seen=seen,
            )
        return result

    def _sample_field(
        self,
        field,
        *,
        depth: int,
        seen: set[str],
    ) -> Any:
        if field.enum_type is not None and field.enum_type.values:
            return field.enum_type.values[0].name
        if field.message_type is not None:
            return self.sample_message(
                field.message_type.full_name,
                depth=depth - 1,
                _seen=seen,
            )
        type_name = field.type
        if type_name == field.TYPE_STRING:
            return ""
        if type_name == field.TYPE_BOOL:
            return False
        if type_name in (
            field.TYPE_INT64,
            field.TYPE_UINT64,
            field.TYPE_SINT64,
            field.TYPE_FIXED64,
            field.TYPE_SFIXED64,
        ):
            return "0"
        if type_name in (
            field.TYPE_DOUBLE,
            field.TYPE_FLOAT,
        ):
            return 0.0
        if type_name == field.TYPE_BYTES:
            return ""
        return 0


__all__ = [
    "GrpcDescriptorError",
    "GrpcDescriptorIndex",
    "GrpcMethod",
]
