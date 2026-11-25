"""Notification button endpoint: POST /api/entries/{id}/action/{action} with the token as body."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .. import repo, tokens
from ..db import utcnow, utcnow_iso
from ..util import fmt_date

log = logging.getLogger("certtracker.actions")


async def notification_action(request: Request):
    try:
        entry_id = int(request.path_params["entry_id"])
    except (KeyError, ValueError):
        return PlainTextResponse("Not found", status_code=404)
    action = request.path_params.get("action", "")
    if action not in tokens.ACTIONS:
        return PlainTextResponse("Not found", status_code=404)
    body = (await request.body())[:1024].decode("utf-8", "ignore").strip()
    if body.startswith("token="):  # also accept form-encoded bodies
        body = body[len("token="):]
    cfg = request.app.state.cfg
    try:
        nonce = tokens.verify_token(cfg.secret_key, body, entry_id, action, now=time.time())
    except tokens.TokenError as exc:
        log.info("Rejected notification action for entry %s: %s", entry_id, exc)  # never log the token
        return PlainTextResponse("Invalid or expired link", status_code=403)

    if entry_id == 0:  # buttons on the test notification
        return PlainTextResponse("Test OK")

    days = tokens.ACTIONS[action]
    service = request.app.state.service
    with request.app.state.db.tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO action_tokens_used(nonce, used_at) VALUES (?, ?)", (nonce, utcnow_iso()))
        if cur.rowcount == 0:
            return PlainTextResponse("Already done")  # idempotent replay: no second effect
        until = service.today() + timedelta(days=days)
        try:
            repo.snooze_entry(c, entry_id, until, "notification button", source="ntfy")
        except LookupError:
            return PlainTextResponse("Entry no longer exists", status_code=404)
        # prune old nonces (tokens live 48h)
        cutoff = (utcnow() - timedelta(days=3)).isoformat(timespec="seconds")
        c.execute("DELETE FROM action_tokens_used WHERE used_at < ?", (cutoff,))
    return PlainTextResponse(f"Snoozed until {fmt_date(until)}")
