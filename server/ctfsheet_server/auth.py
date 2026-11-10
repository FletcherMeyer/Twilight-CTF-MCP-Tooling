"""Authentication, authorization, CSRF and rate limiting."""
from __future__ import annotations

import hmac
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response

from .config import settings
from .crypto import digest, new_token
from .db import get_db, now

SESSION_COOKIE = "ctfsheet_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Principal:
    user_id: int
    team_id: int
    team_name: str
    username: str
    initials: str
    role: str
    via: str  # 'session' | 'token'
    csrf: str | None = None
    token_id: int | None = None

    @property
    def can_write(self) -> bool:
        return self.role in ("owner", "editor")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers --forwarded-allow-ips=127.0.0.1, so this is
    # the real client address when behind the bundled nginx reverse proxy.
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding-window limiter (single-process; the deployment runs one worker)."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, *keys: str) -> None:
        t = time.monotonic()
        with self._lock:
            for key in keys:
                q = self._hits[key]
                while q and q[0] < t - self.window:
                    q.popleft()
                if len(q) >= self.limit:
                    raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
            for key in keys:
                self._hits[key].append(t)


auth_limiter = RateLimiter(limit=10, window_s=300)
write_limiter = RateLimiter(limit=600, window_s=60)


# --------------------------------------------------------------------------- sessions
def create_session(conn: sqlite3.Connection, response: Response, user_id: int) -> str:
    token = new_token()
    csrf = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?)",
        (digest(token), user_id, csrf, now(), _iso(expires)),
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return csrf


def destroy_session(conn: sqlite3.Connection, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")


def issue_api_token(conn: sqlite3.Connection, user_id: int, label: str) -> dict:
    token = new_token("ctfs_")
    expires = datetime.now(timezone.utc) + timedelta(days=settings.token_days)
    cur = conn.execute(
        "INSERT INTO api_tokens(user_id, token_hash, label, created_at, expires_at) VALUES (?,?,?,?,?)",
        (user_id, digest(token), label[:80] or "token", now(), _iso(expires)),
    )
    return {"id": cur.lastrowid, "token": token, "label": label, "expires_at": _iso(expires)}


_USER_SQL = """
SELECT u.id AS user_id, u.team_id, u.username, u.initials, u.role, t.name AS team_name
FROM users u JOIN teams t ON t.id = u.team_id
WHERE u.id = ? AND u.disabled = 0
"""


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
        row = conn.execute(
            "SELECT id, user_id, last_used_at FROM api_tokens "
            "WHERE token_hash = ? AND (expires_at IS NULL OR expires_at > ?)",
            (digest(token), now()),
        ).fetchone()
        if not row:
            raise HTTPException(401, "Invalid or expired API token")
        user = conn.execute(_USER_SQL, (row["user_id"],)).fetchone()
        if not user:
            raise HTTPException(401, "Account disabled")
        if not row["last_used_at"] or row["last_used_at"][:16] != now()[:16]:
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now(), row["id"]))
            conn.commit()
        return Principal(**dict(user), via="token", token_id=row["id"])

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    sess = conn.execute(
        "SELECT user_id, csrf FROM sessions WHERE token_hash = ? AND expires_at > ?",
        (digest(token), now()),
    ).fetchone()
    if not sess:
        raise HTTPException(401, "Session expired")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, sess["csrf"]):
            raise HTTPException(403, "CSRF token missing or invalid")
    user = conn.execute(_USER_SQL, (sess["user_id"],)).fetchone()
    if not user:
        raise HTTPException(401, "Account disabled")
    return Principal(**dict(user), via="session", csrf=sess["csrf"])


def require_editor(request: Request, p: Principal = Depends(current_user)) -> Principal:
    if not p.can_write:
        raise HTTPException(403, "Your team role is read-only (viewer)")
    write_limiter.check(f"u:{p.user_id}")
    return p


def require_owner(p: Principal = Depends(current_user)) -> Principal:
    if p.role != "owner":
        raise HTTPException(403, "Only the team owner can do that")
    return p
