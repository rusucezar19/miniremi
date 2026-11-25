import time
from datetime import date, timedelta

import pytest

from app import repo, tokens

from .conftest import add_entry

KEY = "k" * 48


def test_valid_token_roundtrip():
    t = tokens.make_token(KEY, 5, "snooze1d")
    assert tokens.verify_token(KEY, t, 5, "snooze1d")


def test_token_expires_after_48h():
    now = time.time()
    t = tokens.make_token(KEY, 5, "snooze1d", now=now)
    assert tokens.verify_token(KEY, t, 5, "snooze1d", now=now + 47 * 3600)
    with pytest.raises(tokens.TokenError, match="expired"):
        tokens.verify_token(KEY, t, 5, "snooze1d", now=now + 48 * 3600 + 1)


def test_token_bound_to_entry_and_action():
    t = tokens.make_token(KEY, 5, "snooze1d")
    with pytest.raises(tokens.TokenError):
        tokens.verify_token(KEY, t, 6, "snooze1d")
    with pytest.raises(tokens.TokenError):
        tokens.verify_token(KEY, t, 5, "snooze1w")


def test_tampered_or_foreign_key_rejected():
    t = tokens.make_token(KEY, 5, "snooze1d")
    forged = t.replace("5.snooze1d", "6.snooze1d", 1)
    for bad in (forged, t[:-2] + "xx", "", "a.b.c", t + "."):
        with pytest.raises(tokens.TokenError):
            tokens.verify_token(KEY, bad, 6 if bad is forged else 5, "snooze1d")
    with pytest.raises(tokens.TokenError):
        tokens.verify_token("other" * 10, t, 5, "snooze1d")


# ---------------------------------------------------------------- endpoint
def _snoozed_until(app, eid):
    with app.state.db.tx() as c:
        return repo.get_entry(c, eid, date.today()).snoozed_until


def test_action_endpoint_snoozes_once_and_is_idempotent(app, client, cfg):
    eid = add_entry(app.state.db, date.today() + timedelta(days=3))
    token = tokens.make_token(cfg.secret_key, eid, "snooze1w")
    url = f"/api/entries/{eid}/action/snooze1w"
    r = client.post(url, content=token, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and r.text.startswith("Snoozed until")
    today = app.state.service.today()
    assert _snoozed_until(app, eid) == today + timedelta(days=7)
    # change the snooze manually, then replay the same token: no effect
    with app.state.db.tx() as c:
        repo.unsnooze_entry(c, eid, "u")
    r2 = client.post(url, content=token)
    assert r2.status_code == 200 and r2.text == "Already done"
    assert _snoozed_until(app, eid) is None
    with app.state.db.tx() as c:
        audit = repo.get_audit(c, eid)
    assert any(a["action"] == "snoozed" and a["actor"] == "notification button" for a in audit)


def test_action_endpoint_rejects_wrong_entry_action_and_expired(app, client, cfg):
    eid = add_entry(app.state.db, date.today())
    other = add_entry(app.state.db, date.today())
    token = tokens.make_token(cfg.secret_key, eid, "snooze1d")
    assert client.post(f"/api/entries/{other}/action/snooze1d", content=token).status_code == 403
    assert client.post(f"/api/entries/{eid}/action/snooze1w", content=token).status_code == 403
    old = tokens.make_token(cfg.secret_key, eid, "snooze1d", now=time.time() - 49 * 3600)
    assert client.post(f"/api/entries/{eid}/action/snooze1d", content=old).status_code == 403
    assert client.post(f"/api/entries/{eid}/action/delete", content=token).status_code == 404
    assert _snoozed_until(app, eid) is None and _snoozed_until(app, other) is None


def test_action_endpoint_post_only(app, client, cfg):
    eid = add_entry(app.state.db, date.today())
    assert client.get(f"/api/entries/{eid}/action/snooze1d").status_code == 405


def test_test_notification_buttons(app, client, cfg):
    token = tokens.make_token(cfg.secret_key, 0, "snooze1d")
    r = client.post("/api/entries/0/action/snooze1d", content=token)
    assert r.status_code == 200 and r.text == "Test OK"


def test_tokens_never_logged(app, client, cfg, caplog):
    eid = add_entry(app.state.db, date.today())
    token = tokens.make_token(cfg.secret_key, eid, "snooze1d")
    caplog.set_level("DEBUG")
    client.post(f"/api/entries/{eid}/action/snooze1d", content=token)
    client.post(f"/api/entries/{eid}/action/snooze1d", content=token + "x")
    assert token not in caplog.text
    sig = token.rsplit(".", 1)[1]
    assert sig not in caplog.text
