"""Data access for columns, entries, audit log, settings and users."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import reminders
from .db import utcnow_iso
from .util import (
    duration_days,
    fmt_date,
    fmt_duration,
    parse_date,
    parse_duration,
    parse_duration_text,
)

COLUMN_TYPES = {
    "text": "Text",
    "longtext": "Long text",
    "number": "Number",
    "date": "Date",
    "select": "Single select",
    "checkbox": "Checkbox",
    "duration": "Duration",
}
PROTECTED_ROLES = {"expiry", "window"}  # renamable, but not deletable and fixed type
ROLE_TYPES = {"expiry": "date", "window": "duration"}
MAX_TEXT = 500
MAX_LONGTEXT = 10000


# ---------------------------------------------------------------- settings
def get_settings(c: sqlite3.Connection) -> dict:
    return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


def set_setting(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- columns
@dataclass
class Column:
    id: int
    name: str
    type: str
    position: int
    options: list = field(default_factory=list)
    role: str | None = None
    default: object = None

    @property
    def key(self) -> str:
        return str(self.id)

    @property
    def protected(self) -> bool:
        return self.role in PROTECTED_ROLES

    @property
    def type_label(self) -> str:
        return COLUMN_TYPES.get(self.type, self.type)


def _col(row) -> Column:
    return Column(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        position=row["position"],
        options=json.loads(row["options"] or "[]"),
        role=row["role"],
        default=json.loads(row["default_value"]) if row["default_value"] else None,
    )


def get_columns(c: sqlite3.Connection) -> list[Column]:
    return [_col(r) for r in c.execute("SELECT * FROM columns ORDER BY position, id")]


def get_column(c: sqlite3.Connection, column_id: int) -> Column | None:
    row = c.execute("SELECT * FROM columns WHERE id = ?", (column_id,)).fetchone()
    return _col(row) if row else None


def column_by_role(columns: list[Column], role: str) -> Column | None:
    return next((col for col in columns if col.role == role), None)


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Column name is required")
    if len(name) > 60:
        raise ValueError("Column name is too long (max 60)")
    return name


def _clean_options(options) -> list[str]:
    seen, out = set(), []
    for opt in options:
        opt = str(opt).strip()[:MAX_TEXT]
        if opt and opt.lower() not in seen:
            seen.add(opt.lower())
            out.append(opt)
    return out


def add_column(c, name: str, ctype: str, options=None) -> int:
    name = _clean_name(name)
    if ctype not in COLUMN_TYPES:
        raise ValueError("Unknown column type")
    pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM columns").fetchone()[0]
    cur = c.execute(
        "INSERT INTO columns(name, type, position, options) VALUES (?,?,?,?)",
        (name, ctype, pos, json.dumps(_clean_options(options or []))),
    )
    return cur.lastrowid


def rename_column(c, column_id: int, name: str) -> None:
    c.execute("UPDATE columns SET name = ? WHERE id = ?", (_clean_name(name), column_id))


def set_column_options(c, column_id: int, options) -> None:
    c.execute("UPDATE columns SET options = ? WHERE id = ?", (json.dumps(_clean_options(options)), column_id))


def move_column(c, column_id: int, direction: int) -> None:
    cols = get_columns(c)
    idx = next((i for i, col in enumerate(cols) if col.id == column_id), None)
    if idx is None:
        raise ValueError("Column not found")
    target = idx + (1 if direction > 0 else -1)
    if 0 <= target < len(cols):
        cols[idx], cols[target] = cols[target], cols[idx]
    for pos, col in enumerate(cols):
        c.execute("UPDATE columns SET position = ? WHERE id = ?", (pos, col.id))


def count_column_values(c, column_id: int) -> int:
    path = f'$."{int(column_id)}"'
    return c.execute(
        "SELECT COUNT(*) FROM entries WHERE json_extract(data, ?) IS NOT NULL AND json_extract(data, ?) != ''",
        (path, path),
    ).fetchone()[0]


def delete_column(c, column_id: int) -> None:
    col = get_column(c, column_id)
    if col is None:
        raise ValueError("Column not found")
    if col.protected:
        raise PermissionError("This column is used by the reminder logic and cannot be deleted")
    c.execute("UPDATE entries SET data = json_remove(data, ?)", (f'$."{int(column_id)}"',))
    c.execute("DELETE FROM columns WHERE id = ?", (column_id,))


def change_column_type(c, column_id: int, new_type: str) -> int:
    """Change type, converting values. Returns how many values could not be converted (cleared)."""
    col = get_column(c, column_id)
    if col is None:
        raise ValueError("Column not found")
    if col.protected:
        raise PermissionError("The type of this column cannot be changed")
    if new_type not in COLUMN_TYPES:
        raise ValueError("Unknown column type")
    if new_type == col.type:
        return 0
    lost = 0
    new_col = Column(col.id, col.name, new_type, col.position, list(col.options), col.role)
    for row in c.execute("SELECT id, data FROM entries").fetchall():
        data = json.loads(row["data"])
        if col.key not in data:
            continue
        as_text = display_value(col, data[col.key])
        try:
            data[col.key] = coerce_value(new_col, as_text)
            if data[col.key] is None:
                del data[col.key]
        except ValueError:
            del data[col.key]
            lost += 1
        c.execute("UPDATE entries SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
    options = new_col.options
    if new_type == "select":
        options = _clean_options(options + [v for v in distinct_values(c, col)])
    c.execute("UPDATE columns SET type = ?, options = ? WHERE id = ?", (new_type, json.dumps(options), column_id))
    return lost


def distinct_values(c, col: Column) -> list[str]:
    path = f'$."{col.id}"'
    rows = c.execute(
        "SELECT DISTINCT json_extract(data, ?) AS v FROM entries WHERE json_extract(data, ?) IS NOT NULL",
        (path, path),
    )
    return [str(r["v"]) for r in rows if r["v"] not in (None, "")]


def count_convertible_losses(c, column_id: int, new_type: str) -> int:
    col = get_column(c, column_id)
    new_col = Column(col.id, col.name, new_type, col.position, list(col.options), col.role)
    lost = 0
    for row in c.execute("SELECT data FROM entries"):
        data = json.loads(row["data"])
        if col.key in data:
            try:
                coerce_value(new_col, display_value(col, data[col.key]))
            except ValueError:
                lost += 1
    return lost


# ---------------------------------------------------------------- values
def coerce_value(col: Column, raw):
    """Validate and convert a submitted value. Returns None for empty."""
    if col.type == "checkbox":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes", "y", "x", "✓")
    if raw is None:
        return None
    if col.type == "duration":
        if isinstance(raw, dict):
            return parse_duration(raw.get("n"), raw.get("unit"))
        if isinstance(raw, (tuple, list)):
            if str(raw[0]).strip() == "":
                return None
            return parse_duration(raw[0], raw[1])
        if str(raw).strip() == "":
            return None
        return parse_duration_text(str(raw))
    text = str(raw).strip()
    if text == "":
        return None
    if col.type == "text":
        if len(text) > MAX_TEXT:
            raise ValueError(f"{col.name}: text is too long (max {MAX_TEXT})")
        return text
    if col.type == "longtext":
        text = str(raw).strip()
        if len(text) > MAX_LONGTEXT:
            raise ValueError(f"{col.name}: text is too long (max {MAX_LONGTEXT})")
        return text
    if col.type == "number":
        try:
            num = float(text.replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{col.name}: '{text}' is not a number") from exc
        if num != num or num in (float("inf"), float("-inf")):
            raise ValueError(f"{col.name}: invalid number")
        return int(num) if num.is_integer() else num
    if col.type == "date":
        try:
            return parse_date(text).isoformat()
        except ValueError as exc:
            raise ValueError(f"{col.name}: {exc}") from exc
    if col.type == "select":
        if len(text) > MAX_TEXT:
            raise ValueError(f"{col.name}: option is too long")
        return text
    raise ValueError(f"Unknown column type {col.type}")


def display_value(col: Column, value) -> str:
    if value is None:
        return ""
    if col.type == "date":
        return fmt_date(value)
    if col.type == "duration":
        return fmt_duration(value)
    if col.type == "checkbox":
        return "Yes" if value else "No"
    return str(value)


def _register_select_option(c, col: Column, value) -> None:
    if col.type == "select" and value and value.lower() not in (o.lower() for o in col.options):
        col.options.append(value)
        set_column_options(c, col.id, col.options)


# ---------------------------------------------------------------- entries
@dataclass
class Entry:
    id: int
    data: dict
    snoozed_until: date | None
    created_at: str
    updated_at: str
    columns: list[Column]
    today: date
    approaching_days: int = 30

    def value(self, col: Column):
        return self.data.get(col.key)

    def display(self, col: Column) -> str:
        return display_value(col, self.data.get(col.key))

    def by_role(self, role: str):
        col = column_by_role(self.columns, role)
        return self.data.get(col.key) if col else None

    def display_by_role(self, role: str) -> str:
        col = column_by_role(self.columns, role)
        return self.display(col) if col else ""

    @property
    def expiry(self) -> date | None:
        try:
            return parse_date(self.by_role("expiry"))
        except ValueError:
            return None

    @property
    def window(self) -> dict | None:
        return self.by_role("window")

    @property
    def window_days(self) -> int:
        return duration_days(self.window)

    @property
    def state(self) -> reminders.ReminderState:
        return reminders.ReminderState(self.expiry, self.window_days, self.snoozed_until)

    @property
    def trigger(self) -> date | None:
        return reminders.trigger_date(self.expiry, self.window_days) if self.expiry else None

    @property
    def status(self) -> str:
        return reminders.status(self.state, self.today, self.approaching_days)

    @property
    def status_label(self) -> str:
        return reminders.STATUS_LABELS[self.status]

    @property
    def remaining(self) -> str:
        return reminders.describe_remaining(self.expiry, self.today) if self.expiry else ""

    @property
    def title(self) -> str:
        t = self.display_by_role("title") or f"Entry #{self.id}"
        sub = self.display_by_role("subtitle")
        return f"{t} - {sub}" if sub else t

    @property
    def center(self) -> str:
        return self.display_by_role("center")

    @property
    def sort_key(self):
        e = self.expiry
        return (e is None, e or date.max, self.id)


def _entry(row, columns, today, approaching_days) -> Entry:
    snooze = row["snoozed_until"]
    return Entry(
        id=row["id"],
        data=json.loads(row["data"] or "{}"),
        snoozed_until=date.fromisoformat(snooze) if snooze else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        columns=columns,
        today=today,
        approaching_days=approaching_days,
    )


def list_entries(c, today: date, approaching_days: int = 30, columns=None) -> list[Entry]:
    columns = columns if columns is not None else get_columns(c)
    rows = c.execute("SELECT * FROM entries").fetchall()
    return sorted((_entry(r, columns, today, approaching_days) for r in rows), key=lambda e: e.sort_key)


def get_entry(c, entry_id: int, today: date, approaching_days: int = 30) -> Entry | None:
    row = c.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return _entry(row, get_columns(c), today, approaching_days) if row else None


def build_data(c, columns: list[Column], form_values: dict, base: dict | None = None) -> dict:
    """form_values maps column key -> raw value. Missing keys keep the base value."""
    data = dict(base or {})
    errors = []
    for col in columns:
        if col.key not in form_values:
            continue
        try:
            val = coerce_value(col, form_values[col.key])
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if val is None or (col.type == "checkbox" and val is False):
            data.pop(col.key, None)
        else:
            data[col.key] = val
            _register_select_option(c, col, val if col.type == "select" else None)
    if errors:
        raise ValueError("; ".join(errors))
    return data


def create_entry(c, data: dict, actor) -> int:
    now = utcnow_iso()
    cur = c.execute("INSERT INTO entries(data, created_at, updated_at) VALUES (?,?,?)", (json.dumps(data), now, now))
    add_audit(c, cur.lastrowid, actor, "created", {})
    return cur.lastrowid


def diff_data(columns: list[Column], old: dict, new: dict) -> list[dict]:
    changes = []
    for col in columns:
        a, b = old.get(col.key), new.get(col.key)
        if a != b:
            changes.append({"field": col.name, "from": display_value(col, a), "to": display_value(col, b)})
    return changes


def update_entry(c, entry_id: int, new_data: dict, actor, action: str = "edited") -> list[dict]:
    row = c.execute("SELECT data FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise LookupError("Entry not found")
    old = json.loads(row["data"])
    changes = diff_data(get_columns(c), old, new_data)
    if changes:
        c.execute("UPDATE entries SET data = ?, updated_at = ? WHERE id = ?", (json.dumps(new_data), utcnow_iso(), entry_id))
        add_audit(c, entry_id, actor, action, {"changes": changes})
    return changes


def delete_entry(c, entry_id: int, actor) -> None:
    c.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    c.execute("DELETE FROM notifications_sent WHERE entry_id = ?", (entry_id,))
    add_audit(c, entry_id, actor, "deleted", {})


def duplicate_entry(c, entry_id: int, actor) -> int:
    row = c.execute("SELECT data FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise LookupError("Entry not found")
    new_id = create_entry(c, json.loads(row["data"]), actor)
    add_audit(c, new_id, actor, "duplicated", {"from_entry": entry_id})
    return new_id


def snooze_entry(c, entry_id: int, until: date, actor, source: str = "web") -> None:
    if c.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone() is None:
        raise LookupError("Entry not found")
    c.execute("UPDATE entries SET snoozed_until = ?, updated_at = ? WHERE id = ?", (until.isoformat(), utcnow_iso(), entry_id))
    add_audit(c, entry_id, actor, "snoozed", {"until": fmt_date(until), "via": source})


def unsnooze_entry(c, entry_id: int, actor) -> None:
    c.execute("UPDATE entries SET snoozed_until = NULL, updated_at = ? WHERE id = ?", (utcnow_iso(), entry_id))
    add_audit(c, entry_id, actor, "snooze cleared", {})


def renew_entry(c, entry_id: int, new_expiry: date, new_window: dict | None, actor, today: date) -> None:
    """Mark as renewed: new expiry (and optional window), clear snooze, reset reminder state."""
    columns = get_columns(c)
    exp_col = column_by_role(columns, "expiry")
    win_col = column_by_role(columns, "window")
    row = c.execute("SELECT data FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise LookupError("Entry not found")
    data = json.loads(row["data"])
    old_expiry = data.get(exp_col.key)
    old_window = data.get(win_col.key)
    data[exp_col.key] = new_expiry.isoformat()
    if new_window is not None:
        data[win_col.key] = new_window
    c.execute(
        "UPDATE entries SET data = ?, snoozed_until = NULL, updated_at = ? WHERE id = ?",
        (json.dumps(data), utcnow_iso(), entry_id),
    )
    # Reset reminder state. Today's row is kept so we never send twice in one day.
    c.execute("DELETE FROM notifications_sent WHERE entry_id = ? AND day != ?", (entry_id, today.isoformat()))
    details = {"from": fmt_date(old_expiry), "to": fmt_date(new_expiry)}
    if new_window is not None and new_window != old_window:
        details["window"] = f"{fmt_duration(old_window)} → {fmt_duration(new_window)}"
    add_audit(c, entry_id, actor, "renewed", details)


# ---------------------------------------------------------------- audit
def actor_of(user) -> tuple[int | None, str]:
    if user is None:
        return None, "system"
    if isinstance(user, str):
        return None, user
    return user["id"], user["username"]


def add_audit(c, entry_id, actor, action: str, details: dict) -> None:
    uid, name = actor_of(actor)
    c.execute(
        "INSERT INTO audit_log(entry_id, user_id, actor, action, details, at) VALUES (?,?,?,?,?,?)",
        (entry_id, uid, name, action, json.dumps(details), utcnow_iso()),
    )


def get_audit(c, entry_id: int) -> list[dict]:
    rows = c.execute("SELECT * FROM audit_log WHERE entry_id = ? ORDER BY id DESC LIMIT 200", (entry_id,))
    return [dict(r, details=json.loads(r["details"])) for r in rows]


# ---------------------------------------------------------------- users
def count_users(c) -> int:
    return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user(c, user_id: int):
    return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(c, username: str):
    return c.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()


def list_users(c):
    return c.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()


def create_user(c, username: str, password_hash: str, is_admin: bool = False, must_change: bool = False) -> int:
    username = (username or "").strip()
    if not username or len(username) > 40 or not all(ch.isalnum() or ch in "._-@" for ch in username):
        raise ValueError("Username must be 1-40 characters: letters, digits, . _ - @")
    if get_user_by_name(c, username):
        raise ValueError("That username already exists")
    cur = c.execute(
        "INSERT INTO users(username, password_hash, is_admin, must_change_password, created_at) VALUES (?,?,?,?,?)",
        (username, password_hash, int(is_admin), int(must_change), utcnow_iso()),
    )
    return cur.lastrowid


def count_active_admins(c) -> int:
    return c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0").fetchone()[0]
