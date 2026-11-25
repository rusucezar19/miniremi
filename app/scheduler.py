"""Daily reminder run: decides when to send, what is due, and records what was sent."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import reminders, repo, tokens
from .db import Database, utcnow_iso
from .notifiers import Action, Notification, Notifier, NotifyError
from .util import fmt_date

log = logging.getLogger("certtracker.scheduler")


def parse_send_time(value: str) -> time:
    try:
        hh, mm = (value or "09:00").split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return time(9, 0)


def get_zone(name: str | None, fallback: str) -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


class ReminderService:
    def __init__(self, db: Database, notifier: Notifier, cfg, sleep=asyncio.sleep):
        self.db = db
        self.notifier = notifier
        self.cfg = cfg
        self.sleep = sleep
        self.lock = asyncio.Lock()
        self.retry_at: datetime | None = None
        self.consecutive_failures = 0
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ time helpers
    def zone(self, settings: dict | None = None) -> ZoneInfo:
        if settings is None:
            with self.db.tx() as c:
                settings = repo.get_settings(c)
        return get_zone(settings.get("timezone"), self.cfg.tz)

    def local_now(self, now: datetime | None = None, settings: dict | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        return now.astimezone(self.zone(settings))

    def today(self, now: datetime | None = None) -> "datetime.date":
        return self.local_now(now).date()

    # ------------------------------------------------------------ scheduling
    async def tick(self, now: datetime | None = None) -> dict | None:
        """Run the daily cycle if the most recent send time has not been handled yet.

        Called at startup (catch-up after downtime) and periodically after that.
        """
        now = now or datetime.now(timezone.utc)
        with self.db.tx() as c:
            settings = repo.get_settings(c)
        local = self.local_now(now, settings)
        send_t = parse_send_time(settings.get("send_time"))
        slot_day = local.date() if local.time() >= send_t else local.date() - timedelta(days=1)
        last = settings.get("last_run_day")
        if last and last >= slot_day.isoformat():
            return None
        if self.retry_at and now < self.retry_at:
            return None
        result = await self.run_cycle(local.date())
        if result["failed"] == 0:
            with self.db.tx() as c:
                repo.set_setting(c, "last_run_day", slot_day.isoformat())
            self.consecutive_failures = 0
            self.retry_at = None
        else:
            self.consecutive_failures += 1
            minutes = min(5 * 2 ** (self.consecutive_failures - 1), 60)
            self.retry_at = now + timedelta(minutes=minutes)
            log.warning("Reminder run had %s failed deliveries; retrying in %s min", result["failed"], minutes)
        return result

    async def run_cycle(self, today) -> dict:
        async with self.lock:
            with self.db.tx() as c:
                settings = repo.get_settings(c)
                approaching = int(settings.get("approaching_days", 30))
                threshold = int(settings.get("summary_threshold", 10))
                entries = repo.list_entries(c, today, approaching)
                users = {r["id"]: r for r in c.execute("SELECT id, username, disabled FROM users")}
                sent_today = {
                    r["entry_id"] for r in c.execute("SELECT entry_id FROM notifications_sent WHERE day = ?", (today.isoformat(),))
                }
            due = [e for e in entries if reminders.is_due(e.state, today)]
            pending = [e for e in due if e.id not in sent_today]
            result = {"day": today.isoformat(), "due": len(due), "pending": len(pending), "sent": 0, "failed": 0,
                      "summary": False, "skipped": 0}
            if not pending:
                return result
            # Each user only gets reminders for their own entries, on their own topic.
            by_owner: dict[int, list[repo.Entry]] = {}
            for e in pending:
                owner = users.get(e.owner_id)
                if owner is None or owner["disabled"]:
                    result["skipped"] += 1
                    continue
                by_owner.setdefault(owner["id"], []).append(e)
            last_error = None
            for owner_id, items in by_owner.items():
                topic = self.cfg.topic_for(users[owner_id]["username"])
                if len(items) > threshold:
                    result["summary"] = True
                    ids = [e.id for e in items]
                    self._claim(today, ids, "summary")
                    try:
                        await self.send_with_retry(self.build_summary(items, today, topic))
                    except NotifyError as exc:
                        self._unclaim(today, ids)
                        result["failed"] += len(items)
                        last_error = str(exc)
                    else:
                        self._mark_sent(today, ids)
                        result["sent"] += len(items)
                    continue
                for e in items:
                    if not self._claim(today, [e.id], "entry"):
                        continue
                    try:
                        await self.send_with_retry(self.build_entry_notification(e, today, topic))
                    except NotifyError as exc:
                        self._unclaim(today, [e.id])
                        result["failed"] += 1
                        last_error = str(exc)
                    else:
                        self._mark_sent(today, [e.id])
                        result["sent"] += 1
            if last_error:
                self.record_delivery(False, last_error)
            elif result["sent"]:
                self.record_delivery(True, f"{result['sent']} reminder(s) sent to {len(by_owner)} user(s)")
            return result

    # ------------------------------------------------------------ bookkeeping
    def _claim(self, today, ids, kind) -> bool:
        claimed = False
        with self.db.tx() as c:
            for entry_id in ids:
                cur = c.execute(
                    "INSERT OR IGNORE INTO notifications_sent(entry_id, day, kind, status, at) VALUES (?,?,?,?,?)",
                    (entry_id, today.isoformat(), kind, "sending", utcnow_iso()),
                )
                claimed = claimed or cur.rowcount > 0
        return claimed

    def _unclaim(self, today, ids) -> None:
        with self.db.tx() as c:
            c.executemany(
                "DELETE FROM notifications_sent WHERE entry_id = ? AND day = ? AND status = 'sending'",
                [(i, today.isoformat()) for i in ids],
            )

    def _mark_sent(self, today, ids) -> None:
        with self.db.tx() as c:
            c.executemany(
                "UPDATE notifications_sent SET status = 'sent', at = ? WHERE entry_id = ? AND day = ?",
                [(utcnow_iso(), i, today.isoformat()) for i in ids],
            )

    def record_delivery(self, ok: bool, detail: str) -> None:
        status = {"at": utcnow_iso(), "ok": ok, "detail": detail[:500], "channel": self.notifier.name}
        with self.db.tx() as c:
            repo.set_setting(c, "last_delivery", json.dumps(status))

    async def send_with_retry(self, notification: Notification) -> None:
        delays = list(self.cfg.retry_delays)
        last: NotifyError | None = None
        for attempt in range(len(delays) + 1):
            try:
                await self.notifier.send(notification)
                return
            except NotifyError as exc:
                last = exc
                log.warning("Notification attempt %s failed: %s", attempt + 1, exc)
                if attempt < len(delays):
                    await self.sleep(delays[attempt])
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------ messages
    def _url(self, path: str) -> str:
        return f"{self.cfg.public_base_url}{path}"

    def _actions(self, entry_id: int) -> list[Action]:
        """Two wide buttons so phones show the labels in full. Longer snoozes: Open → Snooze in the app.

        The "snooze1w" action stays valid in tokens.py so notifications sent before this change keep working.
        """
        key = self.cfg.secret_key
        return [
            Action("http", "Snooze 1 day", self._url(f"/api/entries/{entry_id}/action/snooze1d"),
                   body=tokens.make_token(key, entry_id, "snooze1d"), clear=True),
            Action("view", "Open", self._url(f"/entries/{entry_id}" if entry_id else "/")),
        ]

    def build_entry_notification(self, e: repo.Entry, today, topic: str | None = None) -> Notification:
        overdue = e.expiry is not None and today > e.expiry
        lines = []
        if e.center:
            lines.append(f"Center: {e.center}")
        lines.append(f"Expiry date: {fmt_date(e.expiry)}")
        lines.append(reminders.describe_remaining(e.expiry, today))
        return Notification(
            title=f"Certificate expiring: {e.title}",
            message="\n".join(lines),
            priority=4 if overdue else 3,
            click=self._url(f"/entries/{e.id}"),
            actions=self._actions(e.id),
            tags=["warning"] if overdue else ["hourglass_flowing_sand"],
            topic=topic,
        )

    def build_summary(self, entries: list[repo.Entry], today, topic: str | None = None) -> Notification:
        expired = sum(1 for e in entries if e.expiry and today > e.expiry)
        msg = f"{len(entries)} certificates need attention today"
        if expired:
            msg += f", {expired} already expired"
        msg += ". Open the app to review them."
        return Notification(
            title=f"Certificate reminders: {len(entries)} due",
            message=msg,
            priority=4 if expired else 3,
            click=self._url("/?status=attention"),
            actions=[Action("view", "Open", self._url("/?status=attention"))],
            tags=["warning"] if expired else ["hourglass_flowing_sand"],
            topic=topic,
        )

    def build_test(self, topic: str | None = None) -> Notification:
        return Notification(
            title="Certificate expiring: Test notification",
            message="Center: Test\nThis is a test from Certificate Tracker. The Snooze 1 day button should answer 'Test OK'.",
            priority=3,
            click=self._url("/"),
            actions=self._actions(0),
            tags=["white_check_mark"],
            topic=topic,
        )

    async def send_test(self, topic: str | None = None) -> tuple[bool, str]:
        """Send a test message (to one user's topic) and report the result."""
        try:
            await self.send_with_retry(self.build_test(topic))
        except NotifyError as exc:
            self.record_delivery(False, f"Test: {exc}")
            return False, str(exc)
        self.record_delivery(True, "Test notification sent")
        where = f" to topic {topic}" if topic else ""
        return True, f"Test notification sent via {self.notifier.name}{where}."

    # ------------------------------------------------------------ background loop
    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # never let the loop die
                log.exception("Reminder tick failed")
            await asyncio.sleep(self.cfg.scheduler_interval_seconds)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
