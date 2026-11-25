import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app import repo
from app.notifiers import NtfyNotifier
from app.scheduler import ReminderService

from .conftest import add_entry

BUC = ZoneInfo("Europe/Bucharest")


def at(d: date, hh: int, mm: int = 0, tz=BUC) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)


def run(coro):
    return asyncio.run(coro)


def sent_rows(db):
    with db.tx() as c:
        return [tuple(r) for r in c.execute("SELECT entry_id, day, kind, status FROM notifications_sent ORDER BY day, entry_id")]


def test_daily_repeat_until_handled(db, service, notifier):
    d = date(2026, 9, 16)
    eid = add_entry(db, date(2026, 9, 23))  # trigger 16-09-2026
    for i in range(4):
        r = run(service.tick(at(d + timedelta(days=i), 9, 5)))
        assert r["sent"] == 1
    assert len(notifier.sent) == 4
    assert [row[1] for row in sent_rows(db)] == ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"]
    msg = notifier.sent[0]
    assert msg.title == "Certificate expiring: Cert - Sub"
    assert "Center: North" in msg.message and "Expiry date: 23-09-2026" in msg.message and "7 days left" in msg.message
    assert msg.priority == 3
    assert msg.click == f"http://testserver/entries/{eid}"
    assert [a.label for a in msg.actions] == ["Snooze 1 day", "Open"]


def test_not_due_before_trigger_and_overdue_is_high_priority(db, service, notifier):
    add_entry(db, date(2026, 9, 23))
    assert run(service.tick(at(date(2026, 9, 15), 9, 5)))["sent"] == 0
    run(service.tick(at(date(2026, 9, 26), 9, 5)))
    assert notifier.sent[-1].priority == 4
    assert "EXPIRED 3 days ago" in notifier.sent[-1].message


def test_no_duplicate_sends_same_day(db, service, notifier):
    d = date(2026, 9, 16)
    add_entry(db, date(2026, 9, 23))
    run(service.tick(at(d, 9, 0)))
    run(service.tick(at(d, 9, 1)))
    run(service.tick(at(d, 18, 0)))
    run(service.run_cycle(d))  # even a forced cycle does not resend
    assert len(notifier.sent) == 1


def test_before_send_time_nothing_happens(db, service, notifier):
    d = date(2026, 9, 16)
    add_entry(db, date(2026, 9, 23))
    with db.tx() as c:
        repo.set_setting(c, "last_run_day", (d - timedelta(days=1)).isoformat())
    assert run(service.tick(at(d, 8, 59))) is None
    assert run(service.tick(at(d, 9, 0)))["sent"] == 1


def test_snooze_and_resume(db, service, notifier):
    add_entry(db, date(2026, 9, 23), snoozed_until=date(2026, 9, 18))
    assert run(service.tick(at(date(2026, 9, 16), 9, 5)))["sent"] == 0
    assert run(service.tick(at(date(2026, 9, 17), 9, 5)))["sent"] == 0
    assert run(service.tick(at(date(2026, 9, 18), 9, 5)))["sent"] == 1  # resumes on the snooze date
    assert run(service.tick(at(date(2026, 9, 19), 9, 5)))["sent"] == 1


def test_renewal_resets_reminders(db, service, notifier):
    d = date(2026, 9, 16)
    eid = add_entry(db, date(2026, 9, 23), snoozed_until=None)
    run(service.tick(at(d, 9, 5)))
    run(service.tick(at(d + timedelta(days=1), 9, 5)))
    with db.tx() as c:
        repo.snooze_entry(c, eid, d + timedelta(days=5), "u")
        repo.renew_entry(c, eid, date(2027, 9, 23), {"n": 2, "unit": "weeks"}, "u", d + timedelta(days=1))
        e = repo.get_entry(c, eid, d)
        audit = repo.get_audit(c, eid)
    assert e.snoozed_until is None
    assert e.expiry == date(2027, 9, 23) and e.window_days == 14
    assert audit[0]["action"] == "renewed" and audit[0]["details"]["to"] == "23-09-2027"
    # old days cleared, today's kept (never twice per day)
    assert [r[1] for r in sent_rows(db)] == ["2026-09-17"]
    assert run(service.tick(at(d + timedelta(days=2), 9, 5)))["sent"] == 0
    # the new cycle starts at the new trigger date
    assert run(service.tick(at(date(2027, 9, 9), 9, 5)))["sent"] == 1


def test_catch_up_after_downtime(db, service, notifier):
    d = date(2026, 9, 20)
    add_entry(db, date(2026, 9, 23))
    with db.tx() as c:
        repo.set_setting(c, "last_run_day", "2026-09-17")  # down for 18th and 19th
    # container starts at 07:30, before today's send time: yesterday's run was missed -> send now
    r = run(service.tick(at(d, 7, 30)))
    assert r["sent"] == 1
    # today's regular run must not send that entry a second time
    r2 = run(service.tick(at(d, 9, 5)))
    assert r2["sent"] == 0
    assert len(notifier.sent) == 1
    # first start of a fresh install also runs once (catch-up) without duplicates
    with db.tx() as c:
        c.execute("DELETE FROM settings WHERE key = 'last_run_day'")
    assert run(service.tick(at(d, 10, 0)))["sent"] == 0


def test_summary_above_threshold(db, service, notifier):
    d = date(2026, 9, 16)
    for i in range(12):
        add_entry(db, date(2026, 9, 20), title=f"C{i}")
    add_entry(db, date(2026, 9, 1), title="Old")  # expired
    r = run(service.tick(at(d, 9, 5)))
    assert r["summary"] is True and r["sent"] == 13
    assert len(notifier.sent) == 1
    s = notifier.sent[0]
    assert "13" in s.title and "1 already expired" in s.message and s.priority == 4
    assert all(row[2] == "summary" for row in sent_rows(db))


def test_threshold_not_exceeded_sends_individually(db, service, notifier):
    d = date(2026, 9, 16)
    for i in range(10):
        add_entry(db, date(2026, 9, 20), title=f"C{i}")
    r = run(service.tick(at(d, 9, 5)))
    assert r["summary"] is False and len(notifier.sent) == 10


def test_custom_threshold(db, service, notifier):
    with db.tx() as c:
        repo.set_setting(c, "summary_threshold", "2")
    for i in range(3):
        add_entry(db, date(2026, 9, 20), title=f"C{i}")
    run(service.tick(at(date(2026, 9, 16), 9, 5)))
    assert len(notifier.sent) == 1


def test_timezone_handling(db, cfg, notifier):
    async def no_sleep(_):
        return None
    with db.tx() as c:
        repo.set_setting(c, "timezone", "America/New_York")
        repo.set_setting(c, "last_run_day", "2026-09-15")
    svc = ReminderService(db, notifier, cfg, sleep=no_sleep)
    add_entry(db, date(2026, 9, 23))
    ny = ZoneInfo("America/New_York")
    # 09:00 in Bucharest (UTC+3 in September) is 02:00 in New York: not yet time there.
    assert run(svc.tick(at(date(2026, 9, 16), 9, 0, BUC))) is None
    # 09:00 New York time on 16-09 -> sends, and the local date (16th) is the trigger date
    r = run(svc.tick(at(date(2026, 9, 16), 9, 0, ny)))
    assert r["sent"] == 1 and r["day"] == "2026-09-16"
    # Late evening UTC on the 16th is still the 16th in New York: no second send
    assert run(svc.tick(datetime(2026, 9, 16, 23, 30, tzinfo=timezone.utc))) is None
    # local "today" follows the configured zone, not UTC
    assert svc.today(datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)) == date(2026, 9, 16)


def _mock_ntfy(fail_times: int, calls: list):
    def handler(request: httpx.Request):
        calls.append(request)
        if len(calls) <= fail_times:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"id": "x"})
    return httpx.MockTransport(handler)


def test_retry_after_ntfy_failure(db, cfg):
    calls: list = []
    sleeps: list = []

    async def fake_sleep(s):
        sleeps.append(s)
    notifier = NtfyNotifier("http://ntfy:2586", "certs", "tk_secret", transport=_mock_ntfy(2, calls))
    svc = ReminderService(db, notifier, cfg, sleep=fake_sleep)
    eid = add_entry(db, date(2026, 9, 23))
    r = run(svc.tick(at(date(2026, 9, 16), 9, 5)))
    assert r["sent"] == 1 and len(calls) == 3 and len(sleeps) == 2
    req = calls[-1]
    assert req.headers["authorization"] == "Bearer tk_secret"
    body = json.loads(req.content)
    assert body["topic"] == "certs-admin" and body["priority"] == 3  # the owner's personal topic
    assert body["click"] == f"http://testserver/entries/{eid}"
    http_actions = [a for a in body["actions"] if a["action"] == "http"]
    assert [a["label"] for a in body["actions"]] == ["Snooze 1 day", "Open"]
    assert all(a["method"] == "POST" and a["body"] and a["url"].endswith("snooze1d") for a in http_actions)
    assert all(a["body"] not in a["url"] for a in http_actions)  # token never in the URL
    with db.tx() as c:
        status = json.loads(repo.get_settings(c)["last_delivery"])
    assert status["ok"] is True


def test_persistent_failure_is_retried_later_and_reported(db, cfg):
    calls: list = []

    async def no_sleep(_):
        return None
    notifier = NtfyNotifier("http://ntfy:2586", "certs", "", transport=_mock_ntfy(100, calls))
    svc = ReminderService(db, notifier, cfg, sleep=no_sleep)
    add_entry(db, date(2026, 9, 23))
    t0 = at(date(2026, 9, 16), 9, 5)
    r = run(svc.tick(t0))
    assert r["failed"] == 1 and r["sent"] == 0
    assert len(calls) == 1 + len(cfg.retry_delays)
    assert sent_rows(db) == []  # failure is not recorded as sent
    with db.tx() as c:
        settings = repo.get_settings(c)
    status = json.loads(settings["last_delivery"])
    assert status["ok"] is False and "HTTP 500" in status["detail"]
    assert "last_run_day" not in settings
    # backoff: no new attempt one minute later
    assert run(svc.tick(t0 + timedelta(minutes=1))) is None
    # ntfy recovers; after the backoff the reminder goes out (same day, once)
    svc.notifier = NtfyNotifier("http://ntfy:2586", "certs", "", transport=_mock_ntfy(0, []))
    assert run(svc.tick(t0 + timedelta(minutes=6)))["sent"] == 1


def test_send_test_reports_exact_error(db, cfg):
    async def no_sleep(_):
        return None
    notifier = NtfyNotifier("http://ntfy:2586", "certs", "", transport=_mock_ntfy(100, []))
    svc = ReminderService(db, notifier, cfg, sleep=no_sleep)
    ok, msg = run(svc.send_test())
    assert not ok and "ntfy returned HTTP 500: boom" in msg
