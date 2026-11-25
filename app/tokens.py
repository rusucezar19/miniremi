"""HMAC-signed, single-purpose action tokens for notification buttons.

A token is bound to one entry and one action, expires after 48 hours, and carries
a random nonce so a replayed token is detected (and ignored) by the caller.
Tokens travel in the POST body, never in URLs, so they do not end up in access logs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

TOKEN_TTL_SECONDS = 48 * 3600
ACTIONS = {"snooze1d": 1, "snooze1w": 7}  # action -> snooze days


class TokenError(Exception):
    pass


def _key(secret_key: str) -> bytes:
    return hmac.new(secret_key.encode(), b"certtracker-action-token-v1", hashlib.sha256).digest()


def _sign(secret_key: str, payload: str) -> str:
    mac = hmac.new(_key(secret_key), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def make_token(secret_key: str, entry_id: int, action: str, now: float | None = None, ttl: int = TOKEN_TTL_SECONDS) -> str:
    if action not in ACTIONS:
        raise ValueError("unknown action")
    exp = int((now if now is not None else time.time()) + ttl)
    nonce = secrets.token_urlsafe(12)
    payload = f"{int(entry_id)}.{action}.{exp}.{nonce}"
    return f"{payload}.{_sign(secret_key, payload)}"


def verify_token(secret_key: str, token: str, entry_id: int, action: str, now: float | None = None) -> str:
    """Return the nonce if valid for this entry and action; raise TokenError otherwise."""
    parts = (token or "").strip().split(".")
    if len(parts) != 5:
        raise TokenError("malformed")
    t_entry, t_action, t_exp, nonce, sig = parts
    payload = ".".join(parts[:4])
    if not hmac.compare_digest(sig, _sign(secret_key, payload)):
        raise TokenError("bad signature")
    if t_entry != str(int(entry_id)) or t_action != action:
        raise TokenError("token is not valid for this entry or action")
    try:
        exp = int(t_exp)
    except ValueError as exc:
        raise TokenError("malformed") from exc
    if (now if now is not None else time.time()) > exp:
        raise TokenError("expired")
    return nonce
