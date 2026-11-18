"""gRPC 下游 Mock runtime：gripmock backend、stub 安装与 history 归一化。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

import httpx

from contracts import (
    GrpcMockReply,
    GrpcMockLocator,
    GrpcStatus,
    MockCall,
    MockCallResult,
    MockPlanDep,
    MockSession,
    Transport,
)

from .runtime import MockRuntimeAdapter

_STATUS_TO_CODE = {
    GrpcStatus.OK: 0,
    GrpcStatus.CANCELLED: 1,
    GrpcStatus.UNKNOWN: 2,
    GrpcStatus.INVALID_ARGUMENT: 3,
    GrpcStatus.DEADLINE_EXCEEDED: 4,
    GrpcStatus.NOT_FOUND: 5,
    GrpcStatus.ALREADY_EXISTS: 6,
    GrpcStatus.PERMISSION_DENIED: 7,
    GrpcStatus.RESOURCE_EXHAUSTED: 8,
    GrpcStatus.FAILED_PRECONDITION: 9,
    GrpcStatus.ABORTED: 10,
    GrpcStatus.OUT_OF_RANGE: 11,
    GrpcStatus.UNIMPLEMENTED: 12,
    GrpcStatus.INTERNAL: 13,
    GrpcStatus.UNAVAILABLE: 14,
    GrpcStatus.DATA_LOSS: 15,
    GrpcStatus.UNAUTHENTICATED: 16,
}
_CODE_TO_STATUS = {code: status for status, code in _STATUS_TO_CODE.items()}


class GrpcMockRuntimeError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class GripMockBackend(Protocol):
    def health(self) -> None: ...

    def clear_stubs(self) -> None: ...

    def clear_history(self) -> None: ...

    def install_stubs(self, stubs: list[dict[str, Any]]) -> list[str]: ...

    def history(self, *, limit: int = 10_000) -> list[dict[str, Any]]: ...


class GripMockHttpBackend:
    """gripmock admin REST API 客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                self.base_url + path,
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise GrpcMockRuntimeError(
                "mock_server_unavailable",
                f"{method} {path}: {exc}",
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise GrpcMockRuntimeError(
                "mock_server_unavailable",
                f"{method} {path}: status={response.status_code} "
                f"{response.text[:500]}",
            )
        return response

    def health(self) -> None:
        self._request("GET", "/api/health/readiness")

    def clear_stubs(self) -> None:
        self._request("DELETE", "/api/stubs")

    def clear_history(self) -> None:
        self._request("DELETE", "/api/history")

    def install_stubs(self, stubs: list[dict[str, Any]]) -> list[str]:
        response = self._request("POST", "/api/stubs", json=stubs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GrpcMockRuntimeError(
                "mock_stub_invalid",
                "POST /api/stubs 未返回 JSON",
            ) from exc
        if not isinstance(payload, list) or not all(
            isinstance(item, str) for item in payload
        ):
            raise GrpcMockRuntimeError(
                "mock_stub_invalid",
                f"unexpected stub ids: {payload!r}",
            )
        if len(payload) != len(stubs):
            raise GrpcMockRuntimeError(
                "mock_stub_invalid",
                f"installed={len(payload)} requested={len(stubs)}",
            )
        return payload

    def history(self, *, limit: int = 10_000) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/history", params={"limit": limit})
        try:
            payload = response.json()
        except ValueError as exc:
            raise GrpcMockRuntimeError(
                "mock_server_unavailable",
                "GET /api/history 未返回 JSON",
            ) from exc
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GrpcMockRuntimeError(
                "mock_server_unavailable",
                "history 不是对象数组",
            )
        return payload


class GripMockProcess:
    """gripmock 子进程生命周期；与 REST adapter 分离以便测试和复用。"""

    def __init__(
        self,
        *,
        executable: str | Path,
        descriptor_source: str | Path,
        grpc_port: int,
        http_port: int,
        gateway_port: int,
        grpc_host: str = "127.0.0.1",
        http_host: str = "127.0.0.1",
        gateway_host: str = "127.0.0.1",
        work_dir: str | Path | None = None,
        start_timeout_s: float = 15.0,
    ):
        self.executable = str(Path(executable).expanduser().resolve())
        self.descriptor_source = str(Path(descriptor_source).expanduser().resolve())
        self.grpc_port = grpc_port
        self.http_port = http_port
        self.gateway_port = gateway_port
        self.grpc_host = grpc_host
        self.http_host = http_host
        self.gateway_host = gateway_host
        self.work_dir = Path(work_dir).resolve() if work_dir else Path.cwd()
        self.start_timeout_s = start_timeout_s
        self._process: subprocess.Popen | None = None
        self._stdout = None
        self._stderr = None
        self.backend = GripMockHttpBackend(
            f"http://{http_host}:{http_port}",
            timeout=min(max(start_timeout_s, 1.0), 5.0),
        )

    def command(self) -> list[str]:
        return [self.executable, self.descriptor_source]

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GRPC_HOST": self.grpc_host,
                "GRPC_PORT": str(self.grpc_port),
                "HTTP_HOST": self.http_host,
                "HTTP_PORT": str(self.http_port),
                "GATEWAY_HOST": self.gateway_host,
                "GATEWAY_PORT": str(self.gateway_port),
                "HISTORY_ENABLED": "true",
            }
        )
        return env

    def start(self) -> "GripMockProcess":
        if self._process is not None and self._process.poll() is None:
            return self
        if not Path(self.executable).is_file():
            raise GrpcMockRuntimeError(
                "mock_binary_missing",
                self.executable,
            )
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._stdout = (self.work_dir / "gripmock.stdout.log").open(
            "a", encoding="utf-8"
        )
        self._stderr = (self.work_dir / "gripmock.stderr.log").open(
            "a", encoding="utf-8"
        )
        try:
            self._process = subprocess.Popen(
                self.command(),
                cwd=self.work_dir,
                env=self.environment(),
                stdout=self._stdout,
                stderr=self._stderr,
                text=True,
            )
        except OSError as exc:
            self._close_logs()
            raise GrpcMockRuntimeError(
                "mock_server_unavailable",
                f"start failed: {exc}",
            ) from exc
        deadline = time.monotonic() + self.start_timeout_s
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                code = self._process.returncode
                self.stop()
                raise GrpcMockRuntimeError(
                    "mock_server_unavailable",
                    f"gripmock exited early: {code}",
                )
            try:
                self.backend.health()
                return self
            except GrpcMockRuntimeError:
                time.sleep(0.1)
        self.stop()
        raise GrpcMockRuntimeError(
            "mock_server_unavailable",
            f"readiness timeout after {self.start_timeout_s}s",
        )

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.backend.close()
        self._close_logs()

    def _close_logs(self) -> None:
        for stream in (self._stdout, self._stderr):
            if stream is not None:
                stream.close()
        self._stdout = None
        self._stderr = None


def _grpc_stub(
    grpc_path: str,
    reply: GrpcMockReply,
) -> dict[str, Any]:
    if not grpc_path.startswith("/") or grpc_path.count("/") != 2:
        raise GrpcMockRuntimeError("grpc_path_invalid", grpc_path)
    service, method = grpc_path[1:].split("/", 1)
    output: dict[str, Any] = {
        "data": {} if reply.body is None else reply.body,
        "code": _STATUS_TO_CODE[reply.grpc_status],
        "error": reply.message,
    }
    if reply.metadata:
        output["headers"] = dict(reply.metadata)
    if reply.trailers:
        output["trailers"] = dict(reply.trailers)
    if reply.delay_ms:
        output["delay"] = f"{reply.delay_ms}ms"
    return {
        "service": service,
        "method": method,
        "input": {"equals": {}},
        "output": output,
    }


class GrpcMockRuntimeAdapter(MockRuntimeAdapter):
    """一个 gripmock 实例对应的 gRPC 协议 adapter。"""

    name = "grpc_server"
    transport = Transport.GRPC

    def __init__(
        self,
        backend: GripMockBackend,
        deps: Iterable[MockPlanDep],
    ):
        self.backend = backend
        self._deps = {
            dep.dep_id: dep for dep in deps if dep.transport is Transport.GRPC
        }
        self._branch_by_stub_id: dict[str, str] = {}
        self._stub_ids: list[str] = []

    def start(self) -> None:
        self.backend.health()

    def activate(self, session: MockSession) -> None:
        self.backend.health()
        self.backend.clear_stubs()
        self.backend.clear_history()
        stubs: list[dict[str, Any]] = []
        branches: list[str] = []
        for binding in session.bindings:
            dep = self._deps.get(binding.dep_id)
            if dep is None:
                continue
            if not isinstance(dep.locator, GrpcMockLocator):
                raise GrpcMockRuntimeError(
                    "mock_plan_dep_invalid",
                    binding.dep_id,
                )
            reply = dep.replies.get(binding.branch)
            if not isinstance(reply, GrpcMockReply):
                raise GrpcMockRuntimeError(
                    "mock_branch_not_found",
                    f"{binding.dep_id}/{binding.branch}",
                )
            stubs.append(_grpc_stub(dep.locator.grpc_path, reply))
            branches.append(binding.branch)
        if not stubs:
            self._branch_by_stub_id = {}
            self._stub_ids = []
            return
        ids = self.backend.install_stubs(stubs)
        self._stub_ids = list(ids)
        self._branch_by_stub_id = dict(zip(ids, branches, strict=True))

    def collect(self, session: MockSession) -> list[MockCall]:
        calls: list[MockCall] = []
        for record in self.backend.history():
            service = str(record.get("service") or "")
            method = str(record.get("method") or "")
            if not service or not method:
                continue
            grpc_path = f"/{service}/{method}"
            code = int(record.get("code") or 0)
            stub_id = str(record.get("stubId") or "")
            branch = self._branch_by_stub_id.get(stub_id)
            result = MockCallResult.OK if stub_id else MockCallResult.UNBOUND
            error = None if stub_id else "mock_unexpected_dep"
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            calls.append(
                MockCall(
                    call_id=hashlib.sha256(
                        f"{session.session_id}:{len(calls)}:".encode("utf-8") + encoded
                    ).hexdigest()[:32],
                    session_id=session.session_id,
                    case_id=session.case_id,
                    dep_id=f"grpc:{grpc_path}",
                    transport=Transport.GRPC,
                    method=method,
                    original_path=grpc_path,
                    body_sha256=hashlib.sha256(encoded).hexdigest(),
                    body_bytes=len(encoded),
                    branch=branch,
                    grpc_status=_CODE_TO_STATUS.get(code, GrpcStatus.UNKNOWN),
                    delay_ms=int(record.get("elapsedMs") or 0),
                    result=result,
                    error=error,
                    adapter="gripmock",
                    mode="grpc_server",
                    at=str(record.get("timestamp") or ""),
                )
            )
        return calls

    def deactivate(self, session: MockSession) -> None:
        self.backend.clear_stubs()
        self._branch_by_stub_id = {}
        self._stub_ids = []


__all__ = [
    "GripMockBackend",
    "GripMockHttpBackend",
    "GripMockProcess",
    "GrpcMockRuntimeAdapter",
    "GrpcMockRuntimeError",
]
