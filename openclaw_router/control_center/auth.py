"""In-memory administrator sessions for the localhost Control Center."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AdminSession:
    session_token: str
    csrf_token: str
    expires_at: float


@dataclass(frozen=True)
class _StoredSession:
    csrf_token: str
    csrf_digest: str
    expires_at: float


class AdminAuthService:
    COOKIE_NAME = "llmrouter_admin_session"
    CSRF_HEADER = "x-csrf-token"
    DEFAULT_MAX_ACTIVE_SESSIONS = 128
    DEFAULT_MAX_TRACKED_CLIENTS = 1024

    def __init__(
        self,
        admin_token: Optional[str],
        *,
        session_ttl_seconds: int,
        login_window_seconds: int,
        login_max_attempts: int,
        max_active_sessions: int = DEFAULT_MAX_ACTIVE_SESSIONS,
        max_tracked_clients: int = DEFAULT_MAX_TRACKED_CLIENTS,
        clock=time.monotonic,
    ):
        self._admin_token_digest = _digest(admin_token) if admin_token else ""
        self.session_ttl_seconds = session_ttl_seconds
        self.login_window_seconds = login_window_seconds
        self.login_max_attempts = login_max_attempts
        self.max_active_sessions = max(1, int(max_active_sessions))
        self.max_tracked_clients = max(1, int(max_tracked_clients))
        self._clock = clock
        self._sessions: Dict[str, _StoredSession] = {}
        self._failures: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._admin_token_digest)

    def change_token(self, current: str, new: str) -> bool:
        """Replace the administrator token for the current process."""
        if not isinstance(current, str) or not isinstance(new, str) or not (1 <= len(new) <= 4096):
            return False
        with self._lock:
            if not self._admin_token_digest or not hmac.compare_digest(_digest(current), self._admin_token_digest):
                return False
            self._admin_token_digest = _digest(new)
            self._sessions.clear()
            return True

    def login(self, candidate: str, client_key: str) -> tuple[Optional[AdminSession], str]:
        now = self._clock()
        with self._lock:
            self._prune_failures_locked(now)
            failures = self._failures.get(client_key)
            if failures is None:
                while len(self._failures) >= self.max_tracked_clients:
                    self._failures.popitem(last=False)
                failures = deque()
                self._failures[client_key] = failures
            else:
                self._failures.move_to_end(client_key)
            cutoff = now - self.login_window_seconds
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if len(failures) >= self.login_max_attempts:
                return None, "rate_limited"

            valid = 0 < len(candidate) <= 4096 and bool(
                self._admin_token_digest
            ) and hmac.compare_digest(
                _digest(candidate),
                self._admin_token_digest,
            )
            if not valid:
                failures.append(now)
                return None, "invalid_credentials"

            self._failures.pop(client_key, None)
            self._cleanup_locked(now)
            while len(self._sessions) >= self.max_active_sessions:
                oldest_key = next(iter(self._sessions))
                self._sessions.pop(oldest_key, None)
            session_token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            expires_at = now + self.session_ttl_seconds
            self._sessions[_digest(session_token)] = _StoredSession(
                csrf_token=csrf_token,
                csrf_digest=_digest(csrf_token),
                expires_at=expires_at,
            )
            return AdminSession(session_token, csrf_token, expires_at), "ok"

    def verify(self, session_token: Optional[str], csrf_token: Optional[str] = None) -> bool:
        if not session_token:
            return False
        now = self._clock()
        with self._lock:
            record = self._sessions.get(_digest(session_token))
            if record is None:
                return False
            if record.expires_at <= now:
                self._sessions.pop(_digest(session_token), None)
                return False
            if csrf_token is not None and not hmac.compare_digest(
                record.csrf_digest,
                _digest(csrf_token),
            ):
                return False
            return True

    def csrf_for_session(self, session_token: Optional[str]) -> Optional[str]:
        if not session_token:
            return None
        now = self._clock()
        key = _digest(session_token)
        with self._lock:
            record = self._sessions.get(key)
            if record is None:
                return None
            if record.expires_at <= now:
                self._sessions.pop(key, None)
                return None
            return record.csrf_token

    def logout(self, session_token: Optional[str]) -> None:
        if not session_token:
            return
        with self._lock:
            self._sessions.pop(_digest(session_token), None)

    def active_session_count(self) -> int:
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            return len(self._sessions)

    def _cleanup_locked(self, now: float) -> None:
        expired = [key for key, value in self._sessions.items() if value.expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)

    def _prune_failures_locked(self, now: float) -> None:
        cutoff = now - self.login_window_seconds
        for key, failures in list(self._failures.items()):
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                self._failures.pop(key, None)
