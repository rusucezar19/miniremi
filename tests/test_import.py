"""CSV import and template."""
import csv
import io
from datetime import date

from app import repo

from .conftest import USER_PW, add_entry, create_user, login, page_csrf, user_id

HEADER = "Center,Certificate,Sub-category,Expiry date,Notification window"


def _upload(client, content: bytes, filename="certs.csv"):
    token = page_csrf(client, "/import")
    return client.post("/import", data={"csrf": token}, files={"file": (filename, content, "text/csv")},
                       follow_redirects=False)


def _entries(app, owner="admin"):
    with app.state.db.tx() as c:
        return repo.list_entries(c, date(2026, 1, 1), owner_id=user_id(app.state.db, owner))


def test_template_has_current_columns(client):
    login(client)
    r = client.get("/import/template.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.content.decode("utf-8-sig").strip() == HEADER
    r = client.get("/import/template.csv?sep=semicolon")
    assert r.content.decode("utf-8-sig").strip() == HEADER.replace(",", ";")


def test_import_page_renders_format_help(client):
    login(client)
    html = client.get("/import").text
    assert "DD-MM-YYYY" in html and "Notification window" in html and "empty = 1 week" in html


def test_import_comma_file(app, client):
    login(client)
    body = (HEADER + "\n"
            "Cluj,ISO 9001,Quality,23-09-2026,2 weeks\n"
            "Iasi,Fire safety,,01.03.2027,\n"
            "\n"
            "Iasi,Elevator,Lift 2,2027-05-10,10 days\n").encode("utf-8")
    r = _upload(client, body)
    assert r.status_code == 303
    assert "Imported 3 entries" in client.get("/").text
    es = {e.display_by_role("title"): e for e in _entries(app)}
    assert es["ISO 9001"].expiry == date(2026, 9, 23) and es["ISO 9001"].window_days == 14
    assert es["Fire safety"].window_days == 7  # empty -> default 1 week
    assert es["Elevator"].window_days == 10
    with app.state.db.tx() as c:
        center = repo.column_by_role(repo.get_columns(c), "center")
        audit = repo.get_audit(c, es["ISO 9001"].id)
    assert {"Cluj", "Iasi"} <= set(center.options)  # new select options created
    assert audit[-1]["action"] == "created" and audit[-1]["details"] == {"via": "CSV import"}


def test_import_semicolon_excel_ansi_file(app, client):
    login(client)
    # Excel "CSV" on a Romanian Windows: semicolons and Windows-1250 (cedilla ş/ţ)
    body = ("Center;Certificate;Expiry date\r\nBucureşti;Autorizaţie ISU;15-06-2027\r\n").encode("cp1250")
    r = _upload(client, body)
    assert r.status_code == 303
    (e,) = _entries(app)
    assert e.display_by_role("title") == "Autorizaţie ISU" and e.center == "Bucureşti"
    assert e.window_days == 7  # column missing from the file -> default


def test_any_bad_row_imports_nothing(app, client):
    login(client)
    body = (HEADER + "\nCluj,Good,,23-09-2026,1 week\nCluj,Bad date,,31-02-2026,1 week\nCluj,Bad window,,01-01-2027,soon\n").encode()
    r = _upload(client, body)
    assert r.status_code == 400
    assert "Nothing was imported" in r.text
    assert "Row 3: Expiry date: Invalid date" in r.text and "Row 4: Notification window" in r.text
    assert _entries(app) == []
    with app.state.db.tx() as c:  # select options from the failed import were rolled back too
        assert repo.column_by_role(repo.get_columns(c), "center").options == []


def test_unknown_and_missing_headers(app, client):
    login(client)
    r = _upload(client, b"Certificate,Colour\nX,red\n")
    assert r.status_code == 303
    page = client.get("/").text
    assert "Ignored columns" in page and "Colour" in page
    r = _upload(client, b"Foo,Bar\n1,2\n")
    assert r.status_code == 400 and "None of the column names" in r.text
    assert _upload(client, b"").status_code == 400
    assert "no entries below" in _upload(client, (HEADER + "\n").encode()).text


def test_export_then_import_roundtrip(app, client):
    login(client)
    add_entry(app.state.db, date(2026, 9, 23), title="=SUM(A1)", center="Iași", window=(3, "days"))
    exported = client.get("/export.csv").content
    with app.state.db.tx() as c:
        c.execute("DELETE FROM entries")
    assert _upload(client, exported).status_code == 303
    (e,) = _entries(app)
    assert e.display_by_role("title") == "=SUM(A1)"  # export guard removed again
    assert e.center == "Iași" and e.expiry == date(2026, 9, 23) and e.window_days == 3


def test_imported_entries_belong_to_the_importer(app, client):
    create_user(app, "alice")
    login(client, "alice", USER_PW)
    assert _upload(client, (HEADER + "\nCluj,Mine,,23-09-2026,\n").encode()).status_code == 303
    assert len(_entries(app, "alice")) == 1 and _entries(app, "admin") == []


def test_import_requires_login_and_csrf(client):
    r = client.post("/import", files={"file": ("a.csv", HEADER.encode(), "text/csv")}, follow_redirects=False)
    assert r.status_code == 401
    login(client)
    r = client.post("/import", files={"file": ("a.csv", HEADER.encode(), "text/csv")})
    assert r.status_code == 403


def test_template_columns_follow_table_changes(app, client):
    login(client)
    with app.state.db.tx() as c:
        repo.add_column(c, "Owner", "text")
    rows = list(csv.reader(io.StringIO(client.get("/import/template.csv").content.decode("utf-8-sig"))))
    assert rows[0][-1] == "Owner"
