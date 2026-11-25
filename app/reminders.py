"""Pure reminder logic: no database, no I/O. Easy to test."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

STATUSES = ["expired", "due", "approaching", "ok", "snoozed", "nodate"]
STATUS_LABELS = {
    "expired": "Expired",
    "due": "In notification window",
    "approaching": "Approaching",
    "ok": "OK",
    "snoozed": "Snoozed",
    "nodate": "No expiry date",
}


@dataclass
class ReminderState:
    expiry: date | None
    window_days: int
    snoozed_until: date | None = None


def trigger_date(expiry: date, window_days: int) -> date:
    """Trigger date = expiry date minus notification window."""
    return expiry - timedelta(days=max(0, window_days))


def is_snoozed(state: ReminderState, today: date) -> bool:
    # Reminders pause until the snoozed-until date and resume on that date.
    return state.snoozed_until is not None and today < state.snoozed_until


def is_due(state: ReminderState, today: date) -> bool:
    if state.expiry is None or is_snoozed(state, today):
        return False
    return today >= trigger_date(state.expiry, state.window_days)


def status(state: ReminderState, today: date, approaching_days: int = 30) -> str:
    if state.expiry is None:
        return "nodate"
    if is_snoozed(state, today):
        return "snoozed"
    if today > state.expiry:
        return "expired"
    trig = trigger_date(state.expiry, state.window_days)
    if today >= trig:
        return "due"
    if today >= trig - timedelta(days=max(0, approaching_days)):
        return "approaching"
    return "ok"


def days_left(expiry: date, today: date) -> int:
    return (expiry - today).days


def describe_remaining(expiry: date, today: date) -> str:
    left = days_left(expiry, today)
    if left > 1:
        return f"{left} days left"
    if left == 1:
        return "1 day left"
    if left == 0:
        return "Expires today"
    overdue = -left
    return f"EXPIRED {overdue} day{'s' if overdue != 1 else ''} ago"
