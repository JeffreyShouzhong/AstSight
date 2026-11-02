"""用例级 Mock 会话（一个 namespace 同时只允许一个 active session）。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from contracts import MockBinding, MockSession


class SessionError(ValueError):
    def __init__(self, code: str, status: int = 409, detail: str = ""):
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


MAX_SESSION_HISTORY = 10_000


class SessionManager:
    def __init__(self, *, sut_id: str, namespace: str):
        self.sut_id = sut_id
        self.namespace = namespace
        self._lock = threading.Lock()
        self._active: MockSession | None = None
        self._last_closed: MockSession | None = None
        self._expired = False
        self._history: list[MockSession] = []

    def _expire_locked(self) -> None:
        session = self._active
        if session is None:
            return
        try:
            expires = datetime.fromisoformat(session.expires_at)
        except ValueError:
            expires = _now()
        if _now() < expires:
            return
        session.active = False
        session.closed_at = _iso(_now())
        session.close_reason = "ttl_expired"
        self._last_closed = session
        self._active = None
        self._expired = True

    def create(self, payload: dict) -> MockSession:
        if payload.get("namespace") != self.namespace:
            raise SessionError(
                "mock_identity_mismatch",
                409,
                f"namespace={payload.get('namespace')!r}",
            )
        bindings = [MockBinding(**item) for item in payload.get("bindings", [])]
        dep_ids = [binding.dep_id for binding in bindings]
        if len(set(dep_ids)) != len(dep_ids):
            raise SessionError("mock_binding_duplicate", 400)
        ttl_ms = int(payload.get("ttl_ms", 60_000))
        if ttl_ms < 1 or ttl_ms > 3_600_000:
            raise SessionError("invalid_ttl", 400, str(ttl_ms))
        now = _now()
        session = MockSession(
            session_id=str(payload["session_id"]),
            namespace=self.namespace,
            case_id=str(payload["case_id"]),
            sut_id=self.sut_id,
            run_scope=str(payload.get("run_scope", "")),
            bindings=bindings,
            ttl_ms=ttl_ms,
            created_at=_iso(now),
            expires_at=_iso(now + timedelta(milliseconds=ttl_ms)),
        )
        with self._lock:
            self._expire_locked()
            if self._active is not None:
                raise SessionError(
                    "mock_session_conflict", 409, self._active.session_id
                )
            self._active = session
            self._expired = False
            self._history.append(session)
            if len(self._history) > MAX_SESSION_HISTORY:
                del self._history[: len(self._history) - MAX_SESSION_HISTORY]
            return session

    def delete(self, session_id: str, *, reason: str = "closed") -> MockSession:
        with self._lock:
            session = self._active
            if session is None or session.session_id != session_id:
                raise SessionError("mock_session_not_found", 404, session_id)
            session.active = False
            session.closed_at = _iso(_now())
            session.close_reason = reason
            self._active = None
            self._last_closed = session
            return session

    def current(self) -> MockSession | None:
        with self._lock:
            self._expire_locked()
            return self._active

    def last_closed(self) -> MockSession | None:
        """最近关闭/过期的会话；用于把晚到调用归因到正确的用例。"""
        with self._lock:
            self._expire_locked()
            return self._last_closed

    def history(self, *, run_scope: str | None = None) -> list[MockSession]:
        """按创建顺序返回会话快照；可按 run_scope 过滤。"""
        with self._lock:
            self._expire_locked()
            items = [
                session.model_copy(deep=True)
                for session in self._history
                if run_scope is None or session.run_scope == run_scope
            ]
            return items

    @property
    def active_session_id(self) -> str | None:
        session = self.current()
        return session.session_id if session else None

    def for_request(self) -> tuple[MockSession | None, str | None]:
        """返回 (session, error_code)；没有会话时区分 missing/late/expired。"""
        with self._lock:
            self._expire_locked()
            if self._active is not None:
                return self._active, None
            if self._expired:
                self._expired = False
                return None, "mock_session_expired"
            if self._last_closed is not None:
                return None, "mock_late_call"
            return None, "mock_session_missing"

    def binding_map(self, session: MockSession) -> dict[str, str]:
        return {binding.dep_id: binding.branch for binding in session.bindings}
