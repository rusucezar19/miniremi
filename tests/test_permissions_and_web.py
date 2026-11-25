import csv
import io
from datetime import date, timedelta

import pytest

from app import permissions, repo

from .conftest import USER_PW, add_entry, create_user, login, page_csrf


# ---------------------------------------------------------------- permission rules
def _u(is_admin=0, disabled=0):
    return {"id": 1, "username": "x", "is_admin": is_admin, "disabled": disabled}


@pytest.mark.parametrize("perm", ["view_entries", "edit_entries", "export", "view_notification_setup", "change_own_password"])
def test_users_can_work_with_entries(perm):
    assert permissions.can(_u(), perm)
    assert permissions.can(_u(is_admin=1), perm)


@pytest.mark.parametrize("perm", ["manage_columns", "manage_users", "manage_settings"])
def test_structure_and_admin_tasks_are_admin_only(perm):
    assert not permissions.can(_u(), perm)
    assert permissions.can(_u(is_admin=1), perm)


def test_disabled_anonymous_and_unknown():
    assert not permissions.can(None, "view_entries")
    assert not permissions.can(_u(is_admin=1, disabled=1), "view_entries")
    assert not permissions.can(_u(is_admin=1), "no_such_permission")


def test_non_admin_blocked_from_structure_changes(app, client):
    create_user(app, "user1")
    login(client, "user1", USER_PW)
    token = page_csrf(client)
    assert client.get("/columns").status_code == 403
    assert client.get("/users").status_code == 403
    assert client.get("/settings").status_code == 403
    assert client.post("/columns/add", data={"name": "Hack", "type": "text", "csrf": token}).status_code == 403
    assert client.post("/settings/test", data={"csrf": token}).status_code == 403
    with app.state.db.tx() as c:
        assert all(col.name != "Hack" for col in repo.get_columns(c))
    # but entries are editable
    r = client.post("/entries/new", data={"c2": "Mine", "c4": "01-01-2030", "c5_n": "1", "c5_unit": "weeks", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303


# ---------------------------------------------------------------- columns
def test_admin_column_management(app, client):
    login(client)
    token = page_csrf(client, "/columns")
    db = app.state.db
    eid = add_entry(db, date(2030, 1, 1))
    client.post("/columns/add", data={"name": "Owner", "type": "select", "options": "Ann\nBen\nann", "csrf": token})
    with db.tx() as c:
        owner = next(col for col in repo.get_columns(c) if col.name == "Owner")
        exp = repo.column_by_role(repo.get_columns(c), "expiry")
    assert owner.options == ["Ann", "Ben"]
    # rename a system column is allowed
    client.post(f"/columns/{exp.id}", data={"op": "rename", "name": "Valid until", "csrf": token})
    # deleting / retyping it is not
    r = client.post(f"/columns/{exp.id}/delete", data={"csrf": token})
    assert "cannot be deleted" in r.text
    client.post(f"/columns/{exp.id}/type", data={"type": "text", "confirmed": "1", "csrf": token})
    with db.tx() as c:
        exp2 = repo.get_column(c, exp.id)
    assert exp2.name == "Valid until" and exp2.type == "date"
    # delete confirmation states how many entries hold data
    with db.tx() as c:
        cert = repo.column_by_role(repo.get_columns(c), "title")
    r = client.get(f"/columns/{cert.id}/delete")
    assert "1 entry holds data" in r.text
    client.post(f"/columns/{cert.id}/delete", data={"csrf": token})
    with db.tx() as c:
        assert repo.get_column(c, cert.id) is None
        assert cert.key not in repo.get_entry(c, eid, date.today()).data
    # reorder
    with db.tx() as c:
        first = repo.get_columns(c)[0]
    client.post(f"/columns/{first.id}", data={"op": "down", "csrf": token})
    with db.tx() as c:
        assert repo.get_columns(c)[1].id == first.id


def test_change_type_converts_and_warns(app, client):
    login(client)
    token = page_csrf(client, "/columns")
    db = app.state.db
    with db.tx() as c:
        cid = repo.add_column(c, "Count", "text")
        col = repo.get_column(c, cid)
    for v in ("5", "abc"):
        eid = add_entry(db, date(2030, 1, 1))
        with db.tx() as c:
            e = repo.get_entry(c, eid, date.today())
            repo.update_entry(c, eid, dict(e.data, **{col.key: v}), "t")
    r = client.post(f"/columns/{cid}/type", data={"type": "number", "csrf": token})
    assert "1 value(s) cannot be converted" in r.text
    client.post(f"/columns/{cid}/type", data={"type": "number", "confirmed": "1", "csrf": token})
    with db.tx() as c:
        vals = sorted(str(e.data.get(col.key)) for e in repo.list_entries(c, date.today()))
    assert vals == ["5", "None"]


# ---------------------------------------------------------------- entries via the web
def test_entry_crud_audit_and_views(app, client):
    login(client)
    token = page_csrf(client)
    r = client.post("/entries/new", data={"c1": "Cluj", "c2": "ISO 9001", "c3": "Quality", "c4": "23-09-2026",
                                          "c5_n": "1", "c5_unit": "weeks", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    eid = int(r.headers["location"].rsplit("/", 1)[1])
    detail = client.get(f"/entries/{eid}").text
    assert "ISO 9001 - Quality" in detail and "23-09-2026" in detail and "16-09-2026" in detail
    # new select option created on the fly
    with app.state.db.tx() as c:
        assert "Cluj" in repo.column_by_role(repo.get_columns(c), "center").options
    # edit
    client.post(f"/entries/{eid}/edit", data={"c1": "Cluj", "c2": "ISO 9001", "c3": "Env", "c4": "23-09-2026",
                                              "c5_n": "2", "c5_unit": "weeks", "csrf": token})
    # invalid input is rejected and escaped
    r = client.post(f"/entries/{eid}/edit", data={"c1": "<script>x</script>", "c2": "a", "c4": "99-99-2026", "c5_n": "1", "c5_unit": "weeks", "csrf": token})
    assert "<script>x" not in r.text
    with app.state.db.tx() as c:
        e = repo.get_entry(c, eid, date.today())
        audit = repo.get_audit(c, eid)
    assert e.window_days == 14 and e.display_by_role("subtitle") == "Env"
    edits = [a for a in audit if a["action"] == "edited"]
    assert edits and {"field": "Sub-category", "from": "Quality", "to": "Env"} in edits[0]["details"]["changes"]
    assert edits[0]["actor"] == "admin"
    # views
    for view in ("cards", "grouped", "timeline", "table"):
        r = client.get(f"/?view={view}")
        assert r.status_code == 200 and "ISO 9001" in r.text, view
    assert "ISO 9001" in client.get("/?q=iso&status=&center=Cluj").text
    assert "ISO 9001" not in client.get("/?q=nothing-matches").text
    # duplicate & delete
    r = client.post(f"/entries/{eid}/duplicate", data={"csrf": token}, follow_redirects=False)
    dup = int(r.headers["location"].rsplit("/", 1)[1])
    assert dup != eid
    assert "cannot be undone" in client.get(f"/entries/{dup}/delete").text
    client.post(f"/entries/{dup}/delete", data={"csrf": token})
    assert client.get(f"/entries/{dup}").status_code == 404


def test_xss_is_escaped(app, client):
    login(client)
    add_entry(app.state.db, app.state.service.today() + timedelta(days=20), title='<img src=x onerror="alert(1)">')
    for view in ("cards", "table", "grouped", "timeline"):
        html = client.get(f"/?view={view}").text
        assert '<img src=x onerror="alert(1)">' not in html
        assert "&lt;img src=x" in html


def test_snooze_bulk_snooze_and_renew_via_web(app, client):
    login(client)
    token = page_csrf(client)
    db = app.state.db
    today = app.state.service.today()
    a = add_entry(db, today + timedelta(days=2))
    b = add_entry(db, today + timedelta(days=3))
    client.post(f"/entries/{a}/snooze", data={"preset": "3:days", "csrf": token})
    client.post("/entries/bulk-snooze", data={"ids": [str(a), str(b)], "preset": "2:weeks", "csrf": token})
    client.post(f"/entries/{b}/snooze", data={"n": "5", "unit": "days", "csrf": token})
    with db.tx() as c:
        ea, eb = repo.get_entry(c, a, today), repo.get_entry(c, b, today)
    assert ea.snoozed_until == today + timedelta(days=14)
    assert eb.snoozed_until == today + timedelta(days=5)
    assert ea.status == "snoozed"
    r = client.post(f"/entries/{a}/renew", data={"expiry": "not a date", "csrf": token}, follow_redirects=True)
    assert "Invalid date" in r.text
    client.post(f"/entries/{a}/renew", data={"expiry": (today + timedelta(days=400)).strftime("%d-%m-%Y"),
                                             "window_n": "3", "window_unit": "days", "csrf": token})
    with db.tx() as c:
        ea = repo.get_entry(c, a, today)
    assert ea.snoozed_until is None and ea.window_days == 3 and ea.status in ("ok", "approaching")


def test_renew_presets_on_detail_page(app, client):
    login(client)
    eid = add_entry(app.state.db, date(2028, 2, 29))
    html = client.get(f"/entries/{eid}").text
    assert 'data-expiry="2028-02-29"' in html and "+1 year" in html


def test_inline_field_edit(app, client):
    login(client)
    token = page_csrf(client)
    eid = add_entry(app.state.db, date(2030, 1, 1))
    with app.state.db.tx() as c:
        exp = repo.column_by_role(repo.get_columns(c), "expiry")
        win = repo.column_by_role(repo.get_columns(c), "window")
    h = {"X-CSRF-Token": token}
    r = client.post(f"/entries/{eid}/field", json={"col": exp.id, "value": "05-03-2031"}, headers=h)
    assert r.json()["display"] == "05-03-2031"
    r = client.post(f"/entries/{eid}/field", json={"col": win.id, "value": "3 weeks"}, headers=h)
    assert r.json()["display"] == "3 weeks"
    r = client.post(f"/entries/{eid}/field", json={"col": exp.id, "value": "garbage"}, headers=h)
    assert r.status_code == 400 and not r.json()["ok"]
    assert client.post(f"/entries/{eid}/field", json={"col": exp.id, "value": "01-01-2031"}).status_code == 403


def test_csv_export(app, client):
    login(client)
    add_entry(app.state.db, date(2026, 9, 23), title="=cmd|' /C calc'!A0", center="Iași")
    r = client.get("/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["Center", "Certificate", "Sub-category", "Expiry date", "Notification window"]
    assert rows[1][0] == "Iași" and rows[1][3] == "23-09-2026" and rows[1][4] == "1 week"
    assert rows[1][1].startswith("'=")  # formula injection neutralised


def test_settings_save_and_test_button(app, client, notifier):
    login(client)
    token = page_csrf(client, "/settings")
    client.post("/settings/save", data={"send_time": "07:30", "timezone": "Europe/London", "summary_threshold": "5",
                                        "approaching_days": "20", "csrf": token})
    with app.state.db.tx() as c:
        s = repo.get_settings(c)
    assert (s["send_time"], s["timezone"], s["summary_threshold"]) == ("07:30", "Europe/London", "5")
    r = client.post("/settings/test", data={"csrf": token}, follow_redirects=True)
    assert "Test notification sent" in r.text
    assert [a.label for a in notifier.sent[-1].actions] == ["Snooze 1 day", "Open"]
    r = client.post("/settings/save", data={"send_time": "25:00", "timezone": "Nowhere/City", "summary_threshold": "5",
                                            "approaching_days": "20", "csrf": token}, follow_redirects=True)
    assert "Not saved" in r.text


def test_notification_setup_page(tmp_path, notifier):
    from starlette.testclient import TestClient
    from app.main import create_app
    from .conftest import make_cfg
    cfg = make_cfg(tmp_path, ntfy_public_url="https://nas.example.ts.net:10000", ntfy_topic="certs-abc")
    app = create_app(cfg, notifier=notifier, start_scheduler=False)
    with TestClient(app) as client:
        login(client)
        html = client.get("/notifications").text
    assert "ntfy://nas.example.ts.net:10000/certs-abc" in html and "<svg" in html


def test_user_management(app, client):
    login(client)
    token = page_csrf(client, "/users")
    client.post("/users/add", data={"username": "gina", "password": "gina-password", "is_admin": "on", "csrf": token})
    with app.state.db.tx() as c:
        g = repo.get_user_by_name(c, "gina")
        me = repo.get_user_by_name(c, "admin")
    assert g["is_admin"] == 1 and g["must_change_password"] == 1
    client.post(f"/users/{g['id']}", data={"op": "disable", "csrf": token})
    r = client.post(f"/users/{me['id']}", data={"op": "disable", "csrf": token}, follow_redirects=True)
    assert "cannot disable yourself" in r.text
    r = client.post(f"/users/{me['id']}", data={"op": "revoke_admin", "csrf": token}, follow_redirects=True)
    assert "at least one active admin" in r.text


def test_pages_render(client):
    login(client)
    for path in ("/", "/entries/new", "/columns", "/users", "/settings", "/notifications", "/account",
                 "/manifest.webmanifest", "/sw.js", "/healthz", "/static/app.css", "/static/icons/icon-192.png"):
        assert client.get(path).status_code == 200, path
