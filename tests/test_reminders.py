from datetime import date

from app import reminders
from app.reminders import ReminderState, describe_remaining, is_due, status, trigger_date
from app.util import add_years, duration_days, fmt_date, parse_date


def test_trigger_date_spec_example():
    # Expiry 23-09-2026 with a 1 week window triggers on 16-09-2026.
    expiry = parse_date("23-09-2026")
    window = duration_days({"n": 1, "unit": "weeks"})
    assert window == 7
    assert fmt_date(trigger_date(expiry, window)) == "16-09-2026"


def test_trigger_date_days_window_and_zero():
    assert trigger_date(date(2026, 3, 1), 3) == date(2026, 2, 26)
    assert trigger_date(date(2026, 3, 1), 0) == date(2026, 3, 1)


def test_due_from_trigger_date_on_and_after_expiry():
    st = ReminderState(date(2026, 9, 23), 7)
    assert not is_due(st, date(2026, 9, 15))
    assert is_due(st, date(2026, 9, 16))
    assert is_due(st, date(2026, 9, 23))
    assert is_due(st, date(2026, 10, 30))  # keeps going after expiry


def test_status_progression():
    st = ReminderState(date(2026, 9, 23), 7)
    assert status(st, date(2026, 6, 1), 30) == "ok"
    assert status(st, date(2026, 8, 20), 30) == "approaching"
    assert status(st, date(2026, 9, 16), 30) == "due"
    assert status(st, date(2026, 9, 23), 30) == "due"
    assert status(st, date(2026, 9, 24), 30) == "expired"
    assert status(ReminderState(None, 7), date(2026, 1, 1)) == "nodate"


def test_snooze_pauses_until_date_then_resumes():
    st = ReminderState(date(2026, 9, 23), 7, snoozed_until=date(2026, 9, 20))
    assert not is_due(st, date(2026, 9, 19))
    assert status(st, date(2026, 9, 19)) == "snoozed"
    assert is_due(st, date(2026, 9, 20))


def test_describe_remaining():
    exp = date(2026, 9, 23)
    assert describe_remaining(exp, date(2026, 9, 16)) == "7 days left"
    assert describe_remaining(exp, date(2026, 9, 22)) == "1 day left"
    assert describe_remaining(exp, exp) == "Expires today"
    assert describe_remaining(exp, date(2026, 9, 24)) == "EXPIRED 1 day ago"
    assert describe_remaining(exp, date(2026, 9, 28)) == "EXPIRED 5 days ago"


def test_date_parsing_and_formatting():
    assert parse_date("01-02-2026") == date(2026, 2, 1)
    assert parse_date("1/2/2026") == date(2026, 2, 1)
    assert parse_date("2026-02-01") == date(2026, 2, 1)
    assert fmt_date("2026-02-01") == "01-02-2026"
    for bad in ("31-02-2026", "2026/13/01", "tomorrow"):
        try:
            parse_date(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_add_years_leap_day():
    assert add_years(date(2028, 2, 29), 1) == date(2029, 2, 28)
    assert add_years(date(2026, 9, 23), 3) == date(2029, 9, 23)


def test_status_labels_cover_all_statuses():
    assert set(reminders.STATUSES) == set(reminders.STATUS_LABELS)
