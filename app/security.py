"""Passwords, sessions, CSRF and login rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .db import utcnow, utcnow_iso

_hasher = PasswordHasher()  # argon2id with library defaults
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-password")

SESSION_COOKIE = "ct_session"
PRELOGIN_COOKIE = "ct_pre"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_new_password(password: str, min_length: int, confirm: str | None = None) -> None:
    if confirm is not None and password != confirm:
        raise ValueError("The passwords do not match")
    if len(password) < min_length:
        raise ValueError(f"Password must be at least {min_length} characters")
    if len(password) > 256:
        raise ValueError("Password is too long")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(c, user_id: int, days: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (utcnow() + timedelta(days=days)).isoformat(timespec="seconds")
    c.execute(
        "INSERT INTO sessions(id, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?)",
        (_token_hash(token), user_id, secrets.token_urlsafe(32), utcnow_iso(), expires),
    )
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (utcnow_iso(),))
    return token


def load_session(c, token: str | None):
    """Return (session_row, user_row) or (None, None)."""
    if not token:
        return None, None
    row = c.execute(
        "SELECT s.id AS sid, s.csrf, s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None or row["expires_at"] < utcnow_iso() or row["disabled"]:
        return None, None
    return row, row


def delete_session(c, token: str | None) -> None:
    if token:
        c.execute("DELETE FROM sessions WHERE id = ?", (_token_hash(token),))


def delete_user_sessions(c, user_id: int, keep_token: str | None = None) -> None:
    if keep_token:
        c.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, _token_hash(keep_token)))
    else:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def tokens_equal(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


# ---------------------------------------------------------------- login throttling
class IPLimiter:
    """In-memory sliding window of failed logins per client IP."""

    def __init__(self, max_failures: int, window_minutes: int):
        self.max_failures = max_failures
        self.window = window_minutes * 60
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> deque:
        q = self._fails[ip]
        while q and q[0] < now - self.window:
            q.popleft()
        return q

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self._lock:
            return len(self._prune(ip, now)) >= self.max_failures

    def fail(self, ip: str, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._prune(ip, now).append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def check_login(c, username: str, password: str, max_failures: int, lockout_minutes: int, now: datetime | None = None):
    """Return (user_row, error_message). Applies per-account lockout."""
    now = now or datetime.now(timezone.utc)
    user = c.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),)).fetchone()
    generic = "Invalid username or password."
    if user is None:
        verify_password(None, password)  # keep timing similar
        return None, generic
    if user["locked_until"] and user["locked_until"] > now.isoformat(timespec="seconds"):
        verify_password(None, password)
        return None, f"Too many failed attempts. Try again in {lockout_minutes} minutes."
    if not verify_password(user["password_hash"], password) or user["disabled"]:
        fails = user["failed_logins"] + 1
        if fails >= max_failures:
            until = (now + timedelta(minutes=lockout_minutes)).isoformat(timespec="seconds")
            c.execute("UPDATE users SET failed_logins = 0, locked_until = ? WHERE id = ?", (until, user["id"]))
            return None, f"Too many failed attempts. Try again in {lockout_minutes} minutes."
        c.execute("UPDATE users SET failed_logins = ? WHERE id = ?", (fails, user["id"]))
        return None, generic
    c.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user["id"],))
    if _hasher.check_needs_rehash(user["password_hash"]):
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
    return user, None
