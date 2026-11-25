import re
import sys
from datetime import date
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import repo  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.notifiers import ConsoleNotifier  # noqa: E402
from app.scheduler import ReminderService  # noqa: E402

ADMIN_PW = "admin-password-1"
USER_PW = "user-password-1"


def make_cfg(tmp_path, **kw) -> Config:
    base = dict(
        data_dir=tmp_path,
        secret_key="test-secret-" + "x" * 40,
        public_base_url="http://testserver",
        notifier="console",
        retry_delays=(0, 0, 0),
        tz="Europe/Bucharest",
        admin_username="admin",
        admin_password=ADMIN_PW,
    )
    base.update(kw)
    return Config(**base)


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path)
    d.init(cfg.tz)
    return d


@pytest.fixture
def notifier():
    return ConsoleNotifier()


@pytest.fixture
def service(db, notifier, cfg):
    async def no_sleep(_):
        return None
    return ReminderService(db, notifier, cfg, sleep=no_sleep)


def user_id(db, username: str, is_admin: bool = True) -> int:
    """Id of `username`, creating the user (with an unusable password hash) if needed."""
    with db.tx() as c:
        row = repo.get_user_by_name(c, username)
        if row:
            return row["id"]
        return repo.create_user(c, username, "not-a-real-hash", is_admin=is_admin)


def add_entry(db, expiry: date | None, window=(1, "weeks"), title="Cert", sub="Sub", center="North",
              snoozed_until=None, owner="admin") -> int:
    owner_id = user_id(db, owner)
    with db.tx() as c:
        cols = repo.get_columns(c)
        values = {
            repo.column_by_role(cols, "title").key: title,
            repo.column_by_role(cols, "subtitle").key: sub,
            repo.column_by_role(cols, "center").key: center,
            repo.column_by_role(cols, "expiry").key: expiry.isoformat() if expiry else "",
            repo.column_by_role(cols, "window").key: window,
        }
        data = repo.build_data(c, cols, values)
        entry_id = repo.create_entry(c, data, "test", owner_id=owner_id)
        if snoozed_until:
            repo.snooze_entry(c, entry_id, snoozed_until, "test")
    return entry_id


@pytest.fixture
def app(cfg, notifier):
    return create_app(cfg, notifier=notifier, start_scheduler=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


CSRF_RE = re.compile(r'name="csrf(?:-token)?"\s+(?:value|content)="([^"]+)"')


def csrf_from(html: str) -> str:
    m = CSRF_RE.search(html)
    assert m, "no csrf token in page"
    return m.group(1)


def login(client, username="admin", password=ADMIN_PW, expect_ok=True):
    page = client.get("/login")
    token = csrf_from(page.text)
    resp = client.post("/login", data={"username": username, "password": password, "csrf": token}, follow_redirects=False)
    if expect_ok:
        assert resp.status_code == 303, resp.text
    return resp


def page_csrf(client, path="/") -> str:
    return csrf_from(client.get(path).text)


def create_user(app, username, password=USER_PW, is_admin=False):
    from app import security
    with app.state.db.tx() as c:
        return repo.create_user(c, username, security.hash_password(password), is_admin=is_admin)
