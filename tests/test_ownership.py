"""Each user sees only their own entries and only gets reminders for them."""
import asyncio
import csv
import io
import sqlite3
from datetime import date, timedelta

from app import permissions, repo
from app.db import MIGRATIONS, Database

from .conftest import USER_PW, add_entry, create_user, login, make_cfg, page_csrf, user_id
from .test_scheduler import at


def _setup_two_users(app):
    create_user(app, "alice")
    create_user(app, "bob")
    today = app.state.service.today()
    a = add_entry(app.state.db, today + timedelta(days=20), title="Alice cert", owner="alice")
    b = add_entry(app.state.db, today + timedelta(days=20), title="Bob cert", owner="bob")
    return a, b


def test_users_only_see_their_own_entries(app, client):
    a, b = _setup_two_users(app)
    login(client, "alice", USER_PW)
    for view in ("cards", "grouped", "timeline", "table"):
        html = client.get(f"/?view={view}").text
        assert "Alice cert" in html and "Bob cert" not in html, view
    assert client.get(f"/entries/{a}").status_code == 200
    assert client.get(f"/entries/{b}").status_code == 404
    rows = list(csv.reader(io.StringIO(client.get("/export.csv").content.decode("utf-8-sig"))))
    assert len(rows) == 2 and rows[1][1] == "Alice cert"


def test_users_cannot_change_other_users_entries(app, client):
    a, b = _setup_two_users(app)
    login(client, "alice", USER_PW)
    token = page_csrf(client)
    db = app.state.db
    with db.tx() as c:
        exp = repo.column_by_role(repo.get_columns(c), "expiry")
    attempts = [
        client.post(f"/entries/{b}/edit", data={"c2": "hacked", "c4": "01-01-2030", "csrf": token}),
        client.post(f"/entries/{b}/field", json={"col": exp.id, "value": "01-01-2030"}, headers={"X-CSRF-Token": token}),
        client.post(f"/entries/{b}/snooze", data={"preset": "1:weeks", "csrf": token}),
        client.post(f"/entries/{b}/unsnooze", data={"csrf": token}),
        client.post(f"/entries/{b}/renew", data={"expiry": "01-01-2031", "csrf": token}),
        client.post(f"/entries/{b}/duplicate", data={"csrf": token}),
        client.get(f"/entries/{b}/delete"),
        client.post(f"/entries/{b}/delete", data={"csrf": token}),
    ]
    assert all(r.status_code == 404 for r in attempts), [r.status_code for r in attempts]
    client.post("/entries/bulk-snooze", data={"ids": [str(a), str(b)], "preset": "1:weeks", "csrf": token})
    with db.tx() as c:
        eb = repo.get_entry(c, b, date.today())
        ea = repo.get_entry(c, a, date.today())
        n = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert eb is not None and eb.display_by_role("title") == "Bob cert"
    assert eb.snoozed_until is None and eb.expiry != date(2031, 1, 1)
    assert ea.snoozed_until is not None  # her own entry in the same bulk action was snoozed
    assert n == 2


def test_new_and_duplicated_entries_belong_to_the_creator(app, client):
    create_user(app, "alice")
    login(client, "alice", USER_PW)
    token = page_csrf(client)
    r = client.post("/entries/new", data={"c2": "Mine", "c4": "01-01-2030", "c5_n": "1", "c5_unit": "weeks", "csrf": token},
                    follow_redirects=False)
    eid = int(r.headers["location"].rsplit("/", 1)[1])
    r = client.post(f"/entries/{eid}/duplicate", data={"csrf": token}, follow_redirects=False)
    dup = int(r.headers["location"].rsplit("/", 1)[1])
    alice = user_id(app.state.db, "alice")
    with app.state.db.tx() as c:
        owners = {row["id"]: row["owner_id"] for row in c.execute("SELECT id, owner_id FROM entries")}
    assert owners == {eid: alice, dup: alice}


def test_admins_also_see_only_their_own_entries_by_default(app, client):
    a, b = _setup_two_users(app)
    login(client)  # admin
    html = client.get("/?view=cards").text
    assert "Alice cert" not in html and "Bob cert" not in html
    assert client.get(f"/entries/{a}").status_code == 404


def test_view_all_switch_in_permissions(app, client, monkeypatch):
    a, b = _setup_two_users(app)
    monkeypatch.setitem(permissions.PERMISSIONS, "view_all_entries", permissions.ADMIN)
    login(client)
    html = client.get("/?view=cards").text
    assert "Alice cert" in html and "Bob cert" in html
    assert client.get(f"/entries/{b}").status_code == 200


# ---------------------------------------------------------------- notifications
def test_reminders_go_only_to_the_owner(db, service, notifier, cfg):
    alice = user_id(db, "alice", is_admin=False)
    add_entry(db, date(2026, 9, 23), title="A1", owner="alice")
    add_entry(db, date(2026, 9, 23), title="B1", owner="bob")
    r = asyncio.run(service.tick(at(date(2026, 9, 16), 9, 5)))
    assert r["sent"] == 2
    by_topic = {n.topic: n.title for n in notifier.sent}
    assert by_topic == {cfg.topic_for("alice"): "Certificate expiring: A1 - Sub",
                        cfg.topic_for("bob"): "Certificate expiring: B1 - Sub"}
    assert alice


def test_summary_threshold_applies_per_user(db, service, notifier, cfg):
    with db.tx() as c:
        repo.set_setting(c, "summary_threshold", "2")
    for i in range(3):
        add_entry(db, date(2026, 9, 20), title=f"A{i}", owner="alice")
    add_entry(db, date(2026, 9, 20), title="B0", owner="bob")
    asyncio.run(service.tick(at(date(2026, 9, 16), 9, 5)))
    alice_msgs = [n for n in notifier.sent if n.topic == cfg.topic_for("alice")]
    bob_msgs = [n for n in notifier.sent if n.topic == cfg.topic_for("bob")]
    assert len(alice_msgs) == 1 and "3 due" in alice_msgs[0].title
    assert len(bob_msgs) == 1 and bob_msgs[0].title == "Certificate expiring: B0 - Sub"


def test_disabled_users_get_no_reminders(db, service, notifier):
    add_entry(db, date(2026, 9, 23), owner="carol")
    with db.tx() as c:
        c.execute("UPDATE users SET disabled = 1 WHERE username = 'carol'")
    r = asyncio.run(service.tick(at(date(2026, 9, 16), 9, 5)))
    assert r["sent"] == 0 and r["skipped"] == 1 and r["failed"] == 0
    assert not notifier.sent


def test_personal_topic_names(cfg):
    c2 = make_cfg(cfg.data_dir, ntfy_topic="certs-49fd2c76")
    assert c2.topic_for("merlaru") == "certs-49fd2c76-merlaru"
    assert c2.topic_for("Merlaru") == "certs-49fd2c76-merlaru"
    assert c2.topic_for("a.b") != c2.topic_for("a_b")
    for name in ("a.b", "x@y.com", "z" * 40):
        t = c2.topic_for(name)
        assert len(t) <= 64 and all(ch.isalnum() or ch in "-_" for ch in t)


def test_test_notification_goes_to_own_topic(app, client, notifier, cfg):
    create_user(app, "alice")
    login(client, "alice", USER_PW)
    r = client.post("/notifications/test", data={"csrf": page_csrf(client, "/notifications")}, follow_redirects=True)
    assert "Test notification sent" in r.text
    assert notifier.sent[-1].topic == cfg.topic_for("alice")


def test_users_page_shows_topics_and_ntfy_commands(app, client, cfg):
    create_user(app, "merlaru")
    login(client)
    html = client.get("/users").text
    assert cfg.topic_for("merlaru") in html
    assert f"ntfy access merlaru {cfg.topic_for('merlaru')} read-only" in html


# ---------------------------------------------------------------- upgrade of an existing database
def test_migration_assigns_owners_from_history(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO users(id, username, password_hash, is_admin, created_at) VALUES (1, 'boss', 'h', 1, 'x')")
    conn.execute("INSERT INTO users(id, username, password_hash, is_admin, created_at) VALUES (2, 'merlaru', 'h', 0, 'x')")
    for eid in (1, 2):
        conn.execute("INSERT INTO entries(id, data, created_at, updated_at) VALUES (?, '{}', 'x', 'x')", (eid,))
    conn.execute("INSERT INTO audit_log(entry_id, user_id, actor, action, at) VALUES (1, 2, 'merlaru', 'created', 'x')")
    conn.commit()
    conn.close()
    Database(path).init("Europe/Bucharest")
    conn = sqlite3.connect(path)
    owners = dict(conn.execute("SELECT id, owner_id FROM entries"))
    assert owners == {1: 2, 2: 1}  # creator from history; unknown -> first admin
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
