import re

from starlette.testclient import TestClient

from app import repo
from app.main import create_app

from .conftest import ADMIN_PW, USER_PW, create_user, csrf_from, login, make_cfg, page_csrf


def test_login_required(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_logout(client):
    login(client)
    assert client.get("/").status_code == 200
    client.post("/logout", data={"csrf": page_csrf(client)})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_lockout_after_failures(app, client, cfg):
    create_user(app, "bob")
    for _ in range(cfg.login_max_failures - 1):
        r = login(client, "bob", "wrong-password", expect_ok=False)
        assert r.status_code == 401 and "Invalid username or password" in r.text
    r = login(client, "bob", "wrong-password", expect_ok=False)
    assert "Too many failed attempts" in r.text
    # even the right password is refused while locked
    r = login(client, "bob", USER_PW, expect_ok=False)
    assert r.status_code == 401 and "Too many failed attempts" in r.text
    # lock expires
    with app.state.db.tx() as c:
        c.execute("UPDATE users SET locked_until = '2000-01-01T00:00:00+00:00' WHERE username = 'bob'")
    login(client, "bob", USER_PW)


def test_ip_rate_limit(tmp_path, notifier):
    cfg = make_cfg(tmp_path, ip_max_failures=3, trust_proxy=True)
    app = create_app(cfg, notifier=notifier, start_scheduler=False)
    with TestClient(app) as client:
        hdr = {"X-Forwarded-For": "1.2.3.4, 100.64.0.1"}
        for i in range(3):
            token = csrf_from(client.get("/login").text)
            client.post("/login", data={"username": f"u{i}", "password": "x", "csrf": token}, headers=hdr)
        token = csrf_from(client.get("/login").text)
        r = client.post("/login", data={"username": "admin", "password": ADMIN_PW, "csrf": token}, headers=hdr)
        assert r.status_code == 429
        # a different real client IP is not affected (right-most X-Forwarded-For entry is used)
        r = client.post("/login", data={"username": "admin", "password": ADMIN_PW, "csrf": token},
                        headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, follow_redirects=False)
        assert r.status_code == 303


def test_disabled_user_cannot_log_in(app, client):
    uid = create_user(app, "carol")
    with app.state.db.tx() as c:
        c.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    r = login(client, "carol", USER_PW, expect_ok=False)
    assert r.status_code == 401


def test_login_csrf_required(client):
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PW, "csrf": "nope"})
    assert r.status_code == 403


def test_post_without_csrf_is_rejected(client):
    login(client)
    r = client.post("/entries/new", data={"c1": "x"})
    assert r.status_code == 403
    r = client.post("/entries/new", data={"c1": "x", "csrf": page_csrf(client)}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_session_cookie_flags(tmp_path, notifier):
    cfg = make_cfg(tmp_path, public_base_url="https://nas.example.ts.net:8443", cookie_secure=True)
    app = create_app(cfg, notifier=notifier, start_scheduler=False)
    with TestClient(app, base_url="https://testserver") as client:
        token = csrf_from(client.get("/login").text)
        r = client.post("/login", data={"username": "admin", "password": ADMIN_PW, "csrf": token}, follow_redirects=False)
        cookie = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("ct_session=")][0]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")


def test_first_run_setup_requires_code(tmp_path, notifier):
    cfg = make_cfg(tmp_path, admin_username=None, admin_password=None)
    app = create_app(cfg, notifier=notifier, start_scheduler=False)
    code = (tmp_path / "setup-code.txt").read_text().strip()
    with TestClient(app) as client:
        assert client.get("/login", follow_redirects=False).headers["location"] == "/setup"
        token = csrf_from(client.get("/setup").text)
        form = {"username": "boss", "password": "long-enough-pw", "confirm": "long-enough-pw", "csrf": token}
        assert client.post("/setup", data=dict(form, setup_code="wrong")).status_code == 400
        assert client.post("/setup", data=dict(form, setup_code=code, password="short", confirm="short")).status_code == 400
        r = client.post("/setup", data=dict(form, setup_code=code), follow_redirects=False)
        assert r.status_code == 303
        assert not (tmp_path / "setup-code.txt").exists()
        # setup is closed now
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
        login(client, "boss", "long-enough-pw")
    with app.state.db.tx() as c:
        assert repo.get_user_by_name(c, "boss")["is_admin"] == 1


def test_no_default_credentials(tmp_path, notifier):
    cfg = make_cfg(tmp_path, admin_username=None, admin_password=None)
    app = create_app(cfg, notifier=notifier, start_scheduler=False)
    with app.state.db.tx() as c:
        assert repo.count_users(c) == 0


def test_change_own_password_and_min_length(app, client):
    create_user(app, "dave")
    login(client, "dave", USER_PW)
    token = page_csrf(client, "/account")
    r = client.post("/account/password", data={"current": USER_PW, "password": "short", "confirm": "short", "csrf": token})
    assert r.status_code == 400 and "at least" in r.text
    r = client.post("/account/password", data={"current": USER_PW, "password": "brand-new-pass", "confirm": "brand-new-pass", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/logout", data={"csrf": page_csrf(client)})
    login(client, "dave", "brand-new-pass")


def test_admin_reset_forces_password_change(app, client):
    uid = create_user(app, "erin")
    login(client)
    client.post(f"/users/{uid}", data={"op": "reset", "password": "temporary-pass", "csrf": page_csrf(client, "/users")})
    client.post("/logout", data={"csrf": page_csrf(client)})
    login(client, "erin", "temporary-pass")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account"


def test_passwords_are_hashed_with_argon2(app):
    uid = create_user(app, "frank")
    with app.state.db.tx() as c:
        h = repo.get_user(c, uid)["password_hash"]
    assert h.startswith("$argon2") and USER_PW not in h


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert re.search(r"ct_pre=", r.headers.get("set-cookie", ""))
