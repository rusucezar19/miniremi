"""SQLite access: one short-lived connection per unit of work."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        failed_logins INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE columns (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        position INTEGER NOT NULL,
        options TEXT NOT NULL DEFAULT '[]',
        role TEXT UNIQUE,
        default_value TEXT
    );
    CREATE TABLE entries (
        id INTEGER PRIMARY KEY,
        data TEXT NOT NULL DEFAULT '{}',
        snoozed_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER,
        user_id INTEGER,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}',
        at TEXT NOT NULL
    );
    CREATE INDEX audit_entry ON audit_log(entry_id, id);
    CREATE TABLE notifications_sent (
        entry_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (entry_id, day)
    );
    CREATE TABLE action_tokens_used (
        nonce TEXT PRIMARY KEY,
        used_at TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2: every entry belongs to one user (its creator)
    """
    ALTER TABLE entries ADD COLUMN owner_id INTEGER REFERENCES users(id);
    UPDATE entries SET owner_id = (
        SELECT a.user_id FROM audit_log a
        WHERE a.entry_id = entries.id AND a.action = 'created' AND a.user_id IS NOT NULL
        ORDER BY a.id LIMIT 1
    );
    UPDATE entries SET owner_id = (SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1)
        WHERE owner_id IS NULL;
    CREATE INDEX entries_owner ON entries(owner_id);
    """,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def tx(self):
        """Yield a connection; commit on success, roll back on error."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self, default_tz: str) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {i}")
            conn.commit()
        finally:
            conn.close()
        with self.tx() as c:
            seed_defaults(c, default_tz)


DEFAULT_COLUMNS = [
    # name, type, role, default
    ("Center", "select", "center", None),
    ("Certificate", "text", "title", None),
    ("Sub-category", "text", "subtitle", None),
    ("Expiry date", "date", "expiry", None),
    ("Notification window", "duration", "window", {"n": 1, "unit": "weeks"}),
]

DEFAULT_SETTINGS = {
    "send_time": "09:00",
    "summary_threshold": "10",
    "approaching_days": "30",
}


def seed_defaults(c: sqlite3.Connection, default_tz: str) -> None:
    if c.execute("SELECT COUNT(*) FROM columns").fetchone()[0] == 0:
        for pos, (name, ctype, role, default) in enumerate(DEFAULT_COLUMNS):
            c.execute(
                "INSERT INTO columns(name, type, position, options, role, default_value) VALUES (?,?,?,?,?,?)",
                (name, ctype, pos, "[]", role, json.dumps(default) if default is not None else None),
            )
    settings = dict(DEFAULT_SETTINGS, timezone=default_tz)
    for key, value in settings.items():
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
