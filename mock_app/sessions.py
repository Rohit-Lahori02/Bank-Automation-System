"""Server-side session store with idle expiry (in-memory, single process)."""

from __future__ import annotations

import secrets
import time
from threading import Lock

COOKIE_NAME = "LEGACYSESSID"


class SessionStore:
    def __init__(self, idle_seconds: int = 0) -> None:
        self._idle = idle_seconds
        self._sessions: dict[str, dict] = {}
        self._lock = Lock()

    def create(self, user: str) -> str:
        sid = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._sessions[sid] = {"user": user, "created": now, "last_seen": now, "pending": {}}
        return sid

    def get(self, sid: str | None) -> dict | None:
        """Return the live session, refreshing its idle timer; None if missing/expired."""
        if not sid:
            return None
        now = time.time()
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if self._idle and now - sess["last_seen"] > self._idle:
                del self._sessions[sid]
                return None
            sess["last_seen"] = now
            return sess

    def delete(self, sid: str | None) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
