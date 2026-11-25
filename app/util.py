"""Date, duration and value helpers shared by the app and the tests."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

DATE_FORMAT_HINT = "DD-MM-YYYY"
_DMY = re.compile(r"^\s*(\d{1,2})[-./](\d{1,2})[-./](\d{4})\s*$")
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")

DURATION_UNITS = ("days", "weeks")


def parse_date(value: str | date | None) -> date | None:
    """Parse DD-MM-YYYY (also / or . separators) or ISO YYYY-MM-DD. Empty -> None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    value = str(value).strip()
    if not value:
        return None
    m = _ISO.match(value)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DMY.match(value)
        if not m:
            raise ValueError(f"Invalid date '{value}', use {DATE_FORMAT_HINT}")
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'") from exc


def fmt_date(value: str | date | None) -> str:
    if not value:
        return ""
    try:
        d = parse_date(value)
    except ValueError:
        return str(value)
    return d.strftime("%d-%m-%Y") if d else ""


def add_years(d: date, years: int) -> date:
    y = d.year + years
    day = min(d.day, calendar.monthrange(y, d.month)[1])
    return date(y, d.month, day)


def parse_duration(n, unit) -> dict:
    try:
        num = int(str(n).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Duration must be a whole number") from exc
    if num < 0 or num > 3650:
        raise ValueError("Duration must be between 0 and 3650")
    unit = (unit or "days").strip().lower()
    if unit in ("day", "d"):
        unit = "days"
    if unit in ("week", "w"):
        unit = "weeks"
    if unit not in DURATION_UNITS:
        raise ValueError("Duration unit must be days or weeks")
    return {"n": num, "unit": unit}


def parse_duration_text(text: str) -> dict:
    """Parse '2 weeks', '3d', '10 days'."""
    m = re.match(r"^\s*(\d+)\s*([a-zA-Z]*)\s*$", text or "")
    if not m:
        raise ValueError("Use a number plus days or weeks, e.g. '2 weeks'")
    return parse_duration(m.group(1), m.group(2) or "days")


def duration_days(value) -> int:
    if not isinstance(value, dict):
        return 0
    try:
        n = int(value.get("n", 0))
    except (TypeError, ValueError):
        return 0
    return n * 7 if value.get("unit") == "weeks" else n


def fmt_duration(value) -> str:
    if not isinstance(value, dict):
        return ""
    n = value.get("n")
    unit = value.get("unit", "days")
    if n is None:
        return ""
    label = unit[:-1] if int(n) == 1 else unit
    return f"{n} {label}"


def days_between(a: date, b: date) -> int:
    return (b - a).days


def plus_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
