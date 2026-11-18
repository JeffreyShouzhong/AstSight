"""Triple/gRPC-compatible unary 调用。

只处理单请求、单响应和明文 HTTP/2。目标必须是直连 ``tri://`` 或 ``grpc://``
地址，服务契约来自仓库 descriptor；不接 reflection、streaming 或注册中心。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import grpc
from google.protobuf import json_format, message_factory

from ..mock.grpc_descriptor import GrpcDescriptorError, GrpcDescriptorIndex
from .dubbo import DubboCallResult

_METADATA_KEY = re.compile(r"^[0-9a-z_.-]+$")


class TripleUnaryCaller:
    """把用例 JSON 参数映射到 protobuf 动态消息并执行 unary RPC。"""

    def invoke(
        self,
        *,
        interface: str,
        method: str,
        arguments: Sequence[Any],
        url: str,
        descriptor_source: str | Path | None = None,
        grpc_path: str | None = None,
        group: str | None = None,
        version: str | None = None,
        timeout_ms: int = 5_000,
        attachments: Mapping[str, str] | None = None,
        serialization: str | None = None,
        **_ignored: Any,
    ) -> DubboCallResult:
        try:
            target, scheme = _direct_target(url)
        except ValueError as exc:
            return DubboCallResult(
                ok=False,
                rpc_status="BAD_REQUEST",
                error="dubbo_triple_target_invalid",
                message=str(exc),
            )
        if serialization and serialization.lower() not in {
            "protobuf",
            "proto",
        }:
            return DubboCallResult(
                ok=False,
                rpc_status="UNSUPPORTED_PROTOCOL",
                error="dubbo_protocol_unsupported",
                message=f"{scheme} 只支持 protobuf，收到 {serialization}",
            )
        if not descriptor_source:
            return DubboCallResult(
                ok=False,
                rpc_status="UNSUPPORTED_PROTOCOL",
                error="dubbo_triple_descriptor_missing",
                message="Triple-compatible 调用必须提供 descriptor_source",
            )
        path = grpc_path or f"/{interface}/{method}"
        try:
            index = GrpcDescriptorIndex.load(_descriptor_path(descriptor_source))
            descriptor_method = index.method(path)
        except (GrpcDescriptorError, ValueError) as exc:
            return DubboCallResult(
                ok=False,
                rpc_status="UNSUPPORTED_PROTOCOL",
                error="dubbo_triple_descriptor_invalid",
                message=str(exc),
            )
        if descriptor_method.method != method:
            return DubboCallResult(
                ok=False,
                rpc_status="BAD_REQUEST",
                error="dubbo_triple_method_mismatch",
                message=f"grpc_path={path} method={descriptor_method.method}",
            )
        request_type = index.pool.FindMessageTypeByName(
            descriptor_method.request_type
        )
        response_type = index.pool.FindMessageTypeByName(
            descriptor_method.response_type
        )
        request_cls = message_factory.GetMessageClass(request_type)
        response_cls = message_factory.GetMessageClass(response_type)
        if len(arguments) > 1:
            return DubboCallResult(
                ok=False,
                rpc_status="BAD_REQUEST",
                error="dubbo_triple_arguments_invalid",
                message="unary 调用只接受一个 protobuf request 对象",
            )
        try:
            request = request_cls()
            json_format.ParseDict(
                {} if not arguments or arguments[0] is None else arguments[0],
                request,
                ignore_unknown_fields=False,
            )
        except Exception as exc:
            return DubboCallResult(
                ok=False,
                rpc_status="SERIALIZATION",
                error="dubbo_triple_request_invalid",
                exception_type=type(exc).__name__,
                message=str(exc),
            )
        metadata, metadata_error = _metadata(
            attachments or {},
            group=group,
            version=version,
        )
        if metadata_error:
            return DubboCallResult(
                ok=False,
                rpc_status="BAD_REQUEST",
                error="dubbo_triple_metadata_invalid",
                message=metadata_error,
            )

        started = time.monotonic()
        channel = grpc.insecure_channel(target)
        callable_rpc = channel.unary_unary(
            path,
            request_serializer=request.SerializeToString,
            response_deserializer=response_cls.FromString,
        )
        try:
            response = callable_rpc(
                request,
                timeout=max(timeout_ms, 1) / 1000.0,
                metadata=metadata or None,
            )
            body = json_format.MessageToDict(
                response,
                preserving_proto_field_name=True,
            )
            return DubboCallResult(
                ok=True,
                value=body,
                rpc_status="OK",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except grpc.RpcError as exc:
            code = exc.code() if hasattr(exc, "code") else None
            details = exc.details() if hasattr(exc, "details") else str(exc)
            return DubboCallResult(
                ok=False,
                rpc_status=_rpc_status(code),
                error=(
                    code.name.lower()
                    if code is not None and hasattr(code, "name")
                    else "rpc_exception"
                ),
                exception_type=type(exc).__name__,
                message=details,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            channel.close()


def invoke_triple(**kwargs: Any) -> DubboCallResult:
    return TripleUnaryCaller().invoke(**kwargs)


def is_grpc_compatible_target(
    url: str | None,
    *,
    descriptor_source: str | None = None,
    serialization: str | None = None,
) -> bool:
    scheme = str(url or "").partition("://")[0].lower()
    return bool(
        scheme in {"tri", "grpc"}
        and descriptor_source
        and str(serialization or "").lower() in {"protobuf", "proto"}
    )


def _direct_target(url: str) -> tuple[str, str]:
    raw = str(url or "").strip()
    value = raw if "://" in raw else f"tri://{raw}"
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"tri", "grpc"}:
        raise ValueError(f"不支持的 Triple/gRPC scheme: {scheme}")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("Triple/gRPC 目标必须是 scheme://IP:port")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{parsed.port}", scheme


def _descriptor_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


def _metadata(
    attachments: Mapping[str, str],
    *,
    group: str | None,
    version: str | None,
) -> tuple[list[tuple[str, str]], str | None]:
    values = {
        str(key).lower(): str(value)
        for key, value in attachments.items()
    }
    if group:
        values.setdefault("dubbo-group", group)
    if version:
        values.setdefault("dubbo-version", version)
    for key, value in values.items():
        if not _METADATA_KEY.fullmatch(key):
            return [], f"非法 metadata key: {key}"
        if not value.isascii():
            return [], f"metadata value 必须是 ASCII: {key}"
    return sorted(values.items()), None


def _rpc_status(code: grpc.StatusCode | None) -> str:
    if code is None:
        return "RPC_EXCEPTION"
    if code is grpc.StatusCode.DEADLINE_EXCEEDED:
        return "TIMEOUT"
    if code is grpc.StatusCode.UNAVAILABLE:
        return "NETWORK"
    if code is grpc.StatusCode.UNIMPLEMENTED:
        return "UNIMPLEMENTED"
    if code in {
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.ALREADY_EXISTS,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.OUT_OF_RANGE,
    }:
        return "BAD_REQUEST"
    return "RPC_EXCEPTION"


__all__ = [
    "TripleUnaryCaller",
    "invoke_triple",
    "is_grpc_compatible_target",
]
