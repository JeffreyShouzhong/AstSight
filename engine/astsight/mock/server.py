"""内嵌 HTTP Mock 服务（C3.5.5，仅明文 HTTP）。

一个 `MockHttpServer` 对应一个 `sut_id`。服务跑在引擎进程内的后台 uvicorn
线程，端口默认 `bind(127.0.0.1:0)`，实际端口由 `endpoint` 暴露。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from contracts import (
    MockCall,
    MockCallResult,
    MockDrainResult,
    MockMode,
    MockRoute,
    MockRouteMatch,
    MockServer,
    MockServerHealth,
    MockStub,
    MockTransport,
)

from .evidence import MockEvidenceWriter
from .identity import DepIdEntry
from .routes import RouteEntry, RouteResolution, RouteTable
from .sessions import MAX_SESSION_HISTORY, SessionError, SessionManager

_DATA_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_FORBIDDEN_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
_ALLOWED_REQUEST_HEADERS = {"content-type", "accept", "user-agent", "x-request-id"}
_SENSITIVE_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
}


@dataclass(frozen=True)
class MockLimits:
    request_body_bytes: int = 1_048_576
    response_body_bytes: int = 4_194_304
    request_headers_bytes: int = 65_536
    max_concurrency: int = 64
    capture_body_bytes: int = 65_536
    drop_timeout_ms: int = 1_000


class MockResponseError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_headers(headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in _ALLOWED_REQUEST_HEADERS:
            result[lowered] = value
        elif lowered in _SENSITIVE_REQUEST_HEADERS:
            result[lowered] = "<redacted>"
    return result


def _render(value: Any, *, run_scope: str, case_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("{run_scope}", run_scope).replace("{case_id}", case_id)
    if isinstance(value, list):
        return [_render(item, run_scope=run_scope, case_id=case_id) for item in value]
    if isinstance(value, dict):
        return {
            key: _render(item, run_scope=run_scope, case_id=case_id)
            for key, item in value.items()
        }
    return value


def _capture_body(raw: bytes, content_type: str, limit: int) -> Any | None:
    if not raw or len(raw) > limit:
        return None
    lowered = content_type.lower()
    if "json" in lowered:
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
    if lowered.startswith("text/") or not lowered:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _error_response(
    status: int, code: str, *, route_key: str | None = None, dep_id: str | None = None
) -> JSONResponse:
    payload: dict[str, Any] = {"error": code}
    if route_key:
        payload["route_key"] = route_key
    if dep_id:
        payload["dep_id"] = dep_id
    return JSONResponse(payload, status_code=status)


def _build_stub_response(
    stub: MockStub,
    *,
    run_scope: str,
    case_id: str,
    response_limit: int,
) -> Response:
    forbidden = sorted(
        key for key in stub.headers if key.lower() in _FORBIDDEN_RESPONSE_HEADERS
    )
    if forbidden:
        raise MockResponseError(
            "mock_response_invalid", f"forbidden headers: {','.join(forbidden)}"
        )

    headers = dict(stub.headers)
    lowered = {key.lower() for key in headers}
    body: Any = stub.body
    raw: bytes
    if isinstance(body, dict) and set(body) == {"raw_base64"}:
        try:
            raw = base64.b64decode(str(body["raw_base64"]), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MockResponseError("mock_response_invalid", "raw_base64") from exc
        if "content-type" not in lowered:
            headers["Content-Type"] = "application/octet-stream"
    elif isinstance(body, str):
        raw = _render(body, run_scope=run_scope, case_id=case_id).encode("utf-8")
        if "content-type" not in lowered:
            headers["Content-Type"] = "application/json"
    elif body is None:
        raw = b""
    else:
        rendered = _render(body, run_scope=run_scope, case_id=case_id)
        try:
            raw = json.dumps(rendered, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError) as exc:
            raise MockResponseError("mock_response_invalid", "body_not_json") from exc
        if "content-type" not in lowered:
            headers["Content-Type"] = "application/json"

    if len(raw) > response_limit:
        raise MockResponseError(
            "mock_response_invalid", f"body_bytes={len(raw)} limit={response_limit}"
        )
    return Response(content=raw, status_code=stub.status, headers=headers)


class MockHttpServer:
    """内嵌 HTTP Mock 服务；`app` 可直接用于 ASGI 测试。"""

    def __init__(
        self,
        *,
        sut_id: str,
        namespace: str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        history_path: str | Path | None = None,
        capture_bodies: bool = False,
        limits: MockLimits | None = None,
    ):
        self.sut_id = sut_id
        self.namespace = namespace or sut_id
        self.host = host
        self._requested_port = port
        self._port = port
        self.token = token or secrets.token_urlsafe(32)
        self.token_fingerprint = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        self.limits = limits or MockLimits()
        self.capture_bodies = capture_bodies
        self.route_table = RouteTable()
        self.sessions = SessionManager(sut_id=self.sut_id, namespace=self.namespace)
        self.evidence = MockEvidenceWriter(history_path)
        self._app: FastAPI | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._started_monotonic: float | None = None
        self._started_at: str | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._inflight = 0
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------- 路由注册

    def add_route(
        self, route: MockRoute, branches: dict[str, MockStub] | None = None
    ) -> RouteEntry:
        if self._server is not None:
            raise RuntimeError("cannot add route after server start")
        return self.route_table.add_route(route, branches)

    def add_dep(
        self,
        dep: DepIdEntry,
        *,
        route_key: str,
        strategy=None,
        source: str = "profile",
        branches: dict[str, MockStub] | None = None,
    ) -> RouteEntry:
        if self._server is not None:
            raise RuntimeError("cannot add route after server start")
        return self.route_table.add_dep(
            dep,
            route_key=route_key,
            strategy=strategy,
            source=source,
            branches=branches,
        )

    # ------------------------------------------------------------- 生命周期

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self._port}"

    @property
    def admin_endpoint(self) -> str:
        return self.endpoint

    @property
    def started(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def app(self) -> FastAPI:
        if self._app is None:
            self._app = self._build_app()
        return self._app

    def start(self) -> "MockHttpServer":
        if self.started:
            return self
        self._app = self.app
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run, name=f"astsight-http-mock-{self.sut_id}", daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 10.0
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("mock_server_unavailable: uvicorn exited")
            if time.monotonic() > deadline:
                server.should_exit = True
                thread.join(timeout=5)
                raise RuntimeError("mock_server_unavailable: start timeout")
            time.sleep(0.02)
        self._server = server
        self._thread = thread
        self._port = self._bound_port(server) or self._requested_port
        if not self._port:
            self.stop()
            raise RuntimeError("mock_server_unavailable: no bound port")
        self._started_monotonic = time.monotonic()
        self._started_at = _now_iso()
        try:
            response = httpx.get(
                f"{self.endpoint}/_admin/health",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            self.stop()
            raise RuntimeError(f"mock_server_unavailable: {exc}") from exc
        if response.status_code != 200:
            self.stop()
            raise RuntimeError(
                f"mock_server_unavailable: health status={response.status_code}"
            )
        return self

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)

    def __enter__(self) -> "MockHttpServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    @staticmethod
    def _bound_port(server: uvicorn.Server) -> int:
        for item in getattr(server, "servers", []):
            for sock in getattr(item, "sockets", []) or []:
                try:
                    return int(sock.getsockname()[1])
                except (OSError, IndexError, TypeError):
                    continue
        return 0

    # ------------------------------------------------------------- 报告

    def health(self) -> MockServerHealth:
        return MockServerHealth(
            sut_id=self.sut_id,
            namespace=self.namespace,
            route_count=len(self.route_table.entries),
            active_session_id=self.sessions.active_session_id,
            inflight=self._inflight_count(),
            uptime_ms=int((time.monotonic() - self._started_monotonic) * 1000)
            if self._started_monotonic
            else 0,
        )

    def server_model(self) -> MockServer:
        return MockServer(
            transport=MockTransport.HTTP,
            mode=MockMode.EMBEDDED,
            sut_id=self.sut_id,
            namespace=self.namespace,
            endpoint=self.endpoint,
            admin_endpoint=self.admin_endpoint,
            token_fingerprint=self.token_fingerprint,
            route_modes=self.route_table.route_modes,
            route_count=len(self.route_table.entries),
            routes=[
                entry.route.model_copy(deep=True) for entry in self.route_table.entries
            ],
            history_path=(
                str(self.evidence.path.resolve()) if self.evidence.path else None
            ),
            health=self.health() if self._started_monotonic else None,
            started_at=self._started_at,
            pid=os.getpid(),
        )

    # ------------------------------------------------------------- ASGI app

    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="AstSight HTTP Mock",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        self._semaphore = asyncio.Semaphore(self.limits.max_concurrency)

        @app.middleware("http")
        async def _admin_guard(request: Request, call_next):
            path = request.url.path
            if path == "/_admin" or path.startswith("/_admin/"):
                token = self._request_token(request)
                if token is None or not hmac.compare_digest(token, self.token):
                    return _error_response(401, "mock_admin_unauthorized")
            return await call_next(request)

        @app.get("/_admin/health")
        async def _health():
            return self.health().model_dump()

        @app.get("/_admin/server")
        async def _server_info():
            return self.server_model().model_dump(mode="json")

        @app.get("/_admin/sessions")
        async def _list_sessions(request: Request):
            run_scope = request.query_params.get("run_scope") or None
            raw_limit = request.query_params.get("limit", "10000")
            try:
                limit = int(raw_limit)
            except ValueError:
                return _error_response(400, "invalid_limit")
            if limit < 1 or limit > MAX_SESSION_HISTORY:
                return _error_response(400, "invalid_limit")
            sessions = self.sessions.history(run_scope=run_scope)
            truncated = len(sessions) > limit
            selected = sessions[-limit:] if truncated else sessions
            return {
                "count": len(selected),
                "truncated": truncated,
                "sessions": [
                    session.model_dump(mode="json") for session in selected
                ],
            }

        @app.post("/_admin/sessions")
        async def _create_session(request: Request):
            try:
                payload = await request.json()
            except ValueError:
                return _error_response(400, "invalid_json")
            if not isinstance(payload, dict):
                return _error_response(400, "invalid_json")
            try:
                session = self.sessions.create(payload)
            except SessionError as exc:
                return _error_response(exc.status, exc.code, dep_id=exc.detail or None)
            except (KeyError, TypeError, ValueError) as exc:
                return _error_response(400, "invalid_session", dep_id=str(exc))
            return session.model_dump()

        @app.post("/_admin/sessions/{session_id}/drain")
        async def _drain_session(session_id: str, request: Request):
            try:
                payload = await request.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                return _error_response(400, "invalid_json")
            try:
                timeout_ms = int(payload.get("timeout_ms", 200))
            except (TypeError, ValueError):
                return _error_response(400, "invalid_timeout")
            if timeout_ms < 0 or timeout_ms > 60_000:
                return _error_response(400, "invalid_timeout")
            current = self.sessions.current()
            if current is None or current.session_id != session_id:
                return _error_response(
                    404, "mock_session_not_found", dep_id=session_id
                )
            started = time.monotonic()
            deadline = started + timeout_ms / 1000
            inflight = self._inflight_count()
            while inflight and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                inflight = self._inflight_count()
            result = MockDrainResult(
                session_id=session_id,
                drained=inflight == 0,
                inflight=inflight,
                waited_ms=int((time.monotonic() - started) * 1000),
            )
            return result.model_dump()

        @app.delete("/_admin/sessions/{session_id}")
        async def _delete_session(session_id: str):
            try:
                session = self.sessions.delete(session_id)
            except SessionError as exc:
                return _error_response(exc.status, exc.code, dep_id=exc.detail or None)
            return session.model_dump()

        @app.api_route("/_dep/{rest:path}", methods=_DATA_METHODS)
        async def _direct(rest: str, request: Request):
            route_key = rest.split("/", 1)[0] if rest else ""
            return await self._handle(request, route_key or None)

        @app.api_route("/{path:path}", methods=_DATA_METHODS)
        async def _fallback(path: str, request: Request):
            return await self._handle(request, None)

        return app

    @staticmethod
    def _request_token(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.headers.get("x-mock-token")

    # ------------------------------------------------------------- 数据面

    def _inflight_count(self) -> int:
        with self._inflight_lock:
            return self._inflight

    async def _handle(self, request: Request, direct_route_key: str | None):
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.limits.max_concurrency)
        with self._inflight_lock:
            self._inflight += 1
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=0.05)
            except asyncio.TimeoutError:
                return _error_response(503, "mock_server_busy")
            try:
                return await self._handle_locked(request, direct_route_key)
            finally:
                self._semaphore.release()
        finally:
            with self._inflight_lock:
                self._inflight -= 1

    async def _handle_locked(
        self, request: Request, direct_route_key: str | None
    ) -> Response:
        method = request.method.upper()
        path = request.url.path
        # 用于证据归因：即使请求在路由/限额阶段失败，也尽量挂到当前用例上。
        session_hint = self.sessions.current()
        header_bytes = sum(
            len(key.encode("utf-8")) + len(value.encode("utf-8")) + 4
            for key, value in request.headers.items()
        )
        if header_bytes > self.limits.request_headers_bytes:
            self._record_error(
                request, None, session_hint, None, 413, "mock_response_invalid"
            )
            return _error_response(413, "mock_response_invalid")

        body = await request.body()
        if len(body) > self.limits.request_body_bytes:
            self._record_error(
                request, None, session_hint, None, 413, "mock_response_invalid", body=body
            )
            return _error_response(413, "mock_response_invalid")

        resolution = self.route_table.resolve(
            method,
            path,
            host=request.headers.get("host"),
            direct_route_key=direct_route_key,
        )
        if resolution.error:
            route_result = (
                MockCallResult.AMBIGUOUS
                if resolution.error == "mock_route_ambiguous"
                else MockCallResult.UNKNOWN
            )
            self._record_error(
                request,
                resolution,
                session_hint,
                None,
                502,
                resolution.error,
                body=body,
                result=route_result,
            )
            return _error_response(
                502,
                resolution.error,
                route_key=resolution.entry.route.route_key if resolution.entry else None,
                dep_id=resolution.entry.route.dep_id if resolution.entry else None,
            )

        entry = resolution.entry
        assert entry is not None
        session, session_error = self.sessions.for_request()
        if session_error:
            result = (
                MockCallResult.LATE
                if session_error in ("mock_late_call", "mock_session_expired")
                else MockCallResult.UNBOUND
            )
            # 晚到调用必须归因到已关闭的用例，而不是丢成 case_id=None。
            closed = self.sessions.last_closed() if result is MockCallResult.LATE else None
            self._record(
                request,
                resolution,
                closed,
                None,
                502,
                body=body,
                result=result,
                error=session_error,
            )
            return _error_response(
                502,
                session_error,
                route_key=entry.route.route_key,
                dep_id=entry.route.dep_id,
            )

        assert session is not None
        branch = self.sessions.binding_map(session).get(entry.route.dep_id)
        if branch is None:
            self._record(
                request,
                resolution,
                session,
                None,
                502,
                body=body,
                result=MockCallResult.UNBOUND,
                error="mock_unexpected_dep",
            )
            return _error_response(
                502,
                "mock_unexpected_dep",
                route_key=entry.route.route_key,
                dep_id=entry.route.dep_id,
            )

        stub = entry.branch(branch)
        if stub is None:
            self._record(
                request,
                resolution,
                session,
                branch,
                502,
                body=body,
                result=MockCallResult.BRANCH_MISSING,
                error="mock_branch_not_found",
            )
            return _error_response(
                502,
                "mock_branch_not_found",
                route_key=entry.route.route_key,
                dep_id=entry.route.dep_id,
            )

        try:
            response = _build_stub_response(
                stub,
                run_scope=session.run_scope,
                case_id=session.case_id,
                response_limit=self.limits.response_body_bytes,
            )
        except MockResponseError as exc:
            self._record(
                request,
                resolution,
                session,
                branch,
                502,
                body=body,
                result=MockCallResult.UNBOUND,
                error=exc.code,
                delay_ms=stub.delay_ms,
            )
            return _error_response(
                502,
                exc.code,
                route_key=entry.route.route_key,
                dep_id=entry.route.dep_id,
            )

        if stub.delay_ms:
            await asyncio.sleep(stub.delay_ms / 1000)
        if stub.drop_connection:
            # ASGI 层无法强制 TCP reset；按设计退化为“延迟 + 客户端超时”，
            # 证据里明确 drop_mode=timeout，不宣称已 reset。
            await asyncio.sleep(self.limits.drop_timeout_ms / 1000)
            self._record(
                request,
                resolution,
                session,
                branch,
                None,
                body=body,
                result=MockCallResult.OK,
                delay_ms=stub.delay_ms,
                drop_mode="timeout",
                error="mock_connection_dropped",
            )
            return _error_response(
                502,
                "mock_connection_dropped",
                route_key=entry.route.route_key,
                dep_id=entry.route.dep_id,
            )

        self._record(
            request,
            resolution,
            session,
            branch,
            response.status_code,
            body=body,
            result=MockCallResult.OK,
            delay_ms=stub.delay_ms,
        )
        return response

    # ------------------------------------------------------------- 证据

    def _record_error(
        self,
        request: Request,
        resolution: RouteResolution | None,
        session,
        branch: str | None,
        status: int,
        error: str,
        *,
        body: bytes = b"",
        result: MockCallResult = MockCallResult.UNKNOWN,
    ) -> None:
        self._record(
            request,
            resolution,
            session,
            branch,
            status,
            body=body,
            result=result,
            error=error,
        )

    def _record(
        self,
        request: Request,
        resolution: RouteResolution | None,
        session,
        branch: str | None,
        response_status: int | None,
        *,
        body: bytes,
        result: MockCallResult,
        error: str | None = None,
        delay_ms: int = 0,
        drop_mode: str | None = None,
    ) -> None:
        entry = resolution.entry if resolution else None
        content_type = request.headers.get("content-type", "")
        captured = (
            _capture_body(body, content_type, self.limits.capture_body_bytes)
            if self.capture_bodies
            else None
        )
        call = MockCall(
            call_id=secrets.token_hex(16),
            session_id=session.session_id if session else None,
            case_id=session.case_id if session else None,
            route_key=entry.route.route_key if entry else None,
            dep_id=entry.route.dep_id if entry else None,
            route_match=resolution.route_match if resolution else None,
            method=request.method.upper(),
            original_path=request.url.path,
            query_keys=sorted(set(request.query_params.keys())),
            request_headers=_safe_headers(request.headers),
            body_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=len(body),
            request_body=captured,
            branch=branch,
            response_status=response_status,
            delay_ms=delay_ms,
            drop_mode=drop_mode,
            result=result,
            error=error,
            at=_now_iso(),
        )
        self.evidence.write(call)
