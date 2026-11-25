"""Admin pages: table structure (columns), users and settings. Plus notification setup helper."""
from __future__ import annotations

import json
import logging
import secrets
from zoneinfo import ZoneInfo, available_timezones

import segno
from starlette.requests import Request
from urllib.parse import urlsplit

from .. import repo, security
from ..scheduler import parse_send_time
from .common import error_page, int_param, page, redirect, render

log = logging.getLogger("certtracker.admin")


# ---------------------------------------------------------------- columns
@page("manage_columns")
async def columns_page(request: Request):
    with request.app.state.db.tx() as c:
        columns = repo.get_columns(c)
        counts = {col.id: repo.count_column_values(c, col.id) for col in columns}
    return render(request, "columns.html", columns=columns, counts=counts)


@page("manage_columns")
async def column_add(request: Request):
    form = await request.form()
    options = str(form.get("options", "")).splitlines()
    try:
        with request.app.state.db.tx() as c:
            repo.add_column(c, form.get("name", ""), form.get("type", ""), options)
    except ValueError as exc:
        return redirect(request, "/columns", str(exc), "error")
    return redirect(request, "/columns", "Column added.")


@page("manage_columns")
async def column_update(request: Request):
    """Rename, change options, move up/down."""
    column_id = int_param(request, "column_id")
    form = await request.form()
    op = form.get("op")
    try:
        with request.app.state.db.tx() as c:
            col = repo.get_column(c, column_id)
            if col is None:
                return error_page(request, 404, "Column not found.")
            if op == "rename":
                repo.rename_column(c, column_id, form.get("name", ""))
                msg = "Column renamed."
            elif op == "options":
                repo.set_column_options(c, column_id, str(form.get("options", "")).splitlines())
                msg = "Options saved."
            elif op in ("up", "down"):
                repo.move_column(c, column_id, -1 if op == "up" else 1)
                msg = None
            else:
                return error_page(request, 400, "Unknown operation.")
    except ValueError as exc:
        return redirect(request, "/columns", str(exc), "error")
    return redirect(request, "/columns", msg)


@page("manage_columns")
async def column_type(request: Request):
    column_id = int_param(request, "column_id")
    db = request.app.state.db
    form = await request.form()
    new_type = form.get("type", "")
    with db.tx() as c:
        col = repo.get_column(c, column_id)
        if col is None:
            return error_page(request, 404, "Column not found.")
        if col.protected:
            return redirect(request, "/columns", "The type of this column is fixed (used by reminders).", "error")
        if new_type not in repo.COLUMN_TYPES:
            return redirect(request, "/columns", "Unknown type.", "error")
        if not form.get("confirmed"):
            lost = repo.count_convertible_losses(c, column_id, new_type)
            filled = repo.count_column_values(c, column_id)
            msg = f"Change “{col.name}” from {col.type_label} to {repo.COLUMN_TYPES[new_type]}? {filled} entries hold data in this column."
            if lost:
                msg += f" {lost} value(s) cannot be converted and will be cleared."
            return render(request, "confirm.html", title="Change column type", message=msg,
                          action=f"/columns/{column_id}/type", button="Change type", cancel="/columns",
                          hidden={"type": new_type, "confirmed": "1"})
        lost = repo.change_column_type(c, column_id, new_type)
    return redirect(request, "/columns", f"Type changed.{f' {lost} value(s) were cleared.' if lost else ''}")


@page("manage_columns")
async def column_delete(request: Request):
    column_id = int_param(request, "column_id")
    with request.app.state.db.tx() as c:
        col = repo.get_column(c, column_id)
        if col is None:
            return error_page(request, 404, "Column not found.")
        if col.protected:
            return redirect(request, "/columns", "This column is used by the reminder logic and cannot be deleted.", "error")
        if request.method == "GET":
            n = repo.count_column_values(c, column_id)
            return render(request, "confirm.html", title="Delete column",
                          message=f"Delete the column “{col.name}”? {n} entr{'y holds' if n == 1 else 'ies hold'} data in it. That data will be removed. This cannot be undone.",
                          action=f"/columns/{column_id}/delete", button="Delete column", cancel="/columns")
        repo.delete_column(c, column_id)
    return redirect(request, "/columns", "Column deleted.")


# ---------------------------------------------------------------- users
@page("manage_users")
async def users_page(request: Request):
    with request.app.state.db.tx() as c:
        users = repo.list_users(c)
    return render(request, "users.html", users=users, min_len=request.app.state.cfg.min_password_length)


@page("manage_users")
async def user_add(request: Request):
    cfg = request.app.state.cfg
    form = await request.form()
    try:
        security.validate_new_password(str(form.get("password", "")), cfg.min_password_length)
        with request.app.state.db.tx() as c:
            repo.create_user(c, form.get("username", ""), security.hash_password(form["password"]),
                             is_admin=form.get("is_admin") is not None, must_change=True)
    except ValueError as exc:
        return redirect(request, "/users", str(exc), "error")
    return redirect(request, "/users", "User created. They must choose a new password at first login.")


@page("manage_users")
async def user_update(request: Request):
    user_id = int_param(request, "user_id")
    cfg = request.app.state.cfg
    form = await request.form()
    op = form.get("op")
    me = request.state.user
    with request.app.state.db.tx() as c:
        target = repo.get_user(c, user_id)
        if target is None:
            return error_page(request, 404, "User not found.")
        if op == "disable" and target["id"] == me["id"]:
            return redirect(request, "/users", "You cannot disable yourself.", "error")
        removing_admin = op in ("disable", "revoke_admin") and target["is_admin"] and not target["disabled"]
        if removing_admin and repo.count_active_admins(c) <= 1:
            return redirect(request, "/users", "There must be at least one active admin.", "error")
        if op == "disable":
            c.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user_id,))
            security.delete_user_sessions(c, user_id)
            msg = f"{target['username']} disabled."
        elif op == "enable":
            c.execute("UPDATE users SET disabled = 0, failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))
            msg = f"{target['username']} enabled."
        elif op == "reset":
            pw = str(form.get("password", ""))
            try:
                security.validate_new_password(pw, cfg.min_password_length)
            except ValueError as exc:
                return redirect(request, "/users", str(exc), "error")
            c.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 1, failed_logins = 0, locked_until = NULL WHERE id = ?",
                (security.hash_password(pw), user_id),
            )
            security.delete_user_sessions(c, user_id)
            msg = f"Password for {target['username']} reset. They must change it at next login."
        elif op == "make_admin":
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
            msg = f"{target['username']} is now an admin."
        elif op == "revoke_admin":
            c.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
            msg = f"{target['username']} is no longer an admin."
        else:
            return error_page(request, 400, "Unknown operation.")
    return redirect(request, "/users", msg)


# ---------------------------------------------------------------- settings
_TZ_CACHE: list[str] = []


def _timezones() -> list[str]:
    if not _TZ_CACHE:
        _TZ_CACHE.extend(sorted(tz for tz in available_timezones() if "/" in tz and not tz.startswith(("Etc/", "SystemV/", "posix/", "right/"))))
        _TZ_CACHE.append("UTC")
    return _TZ_CACHE


def _settings_ctx(request: Request):
    cfg = request.app.state.cfg
    with request.app.state.db.tx() as c:
        settings = repo.get_settings(c)
    delivery = json.loads(settings["last_delivery"]) if settings.get("last_delivery") else None
    return dict(settings=settings, delivery=delivery, timezones=_timezones(), notifier=request.app.state.notifier.name,
                cfg=cfg, now_local=request.app.state.service.local_now())


@page("manage_settings")
async def settings_page(request: Request):
    return render(request, "settings.html", **_settings_ctx(request))


@page("manage_settings")
async def settings_save(request: Request):
    form = await request.form()
    try:
        send_time = str(form.get("send_time", "")).strip()
        if parse_send_time(send_time).strftime("%H:%M") != send_time.zfill(5):
            raise ValueError("Send time must be HH:MM (24h)")
        tz = str(form.get("timezone", "")).strip()
        ZoneInfo(tz)
        threshold = int(form.get("summary_threshold", "10"))
        approaching = int(form.get("approaching_days", "30"))
        if not (1 <= threshold <= 1000) or not (0 <= approaching <= 365):
            raise ValueError("Threshold must be 1-1000 and approaching days 0-365")
    except (ValueError, KeyError) as exc:
        return redirect(request, "/settings", f"Not saved: {exc}", "error")
    with request.app.state.db.tx() as c:
        old = repo.get_settings(c)
        repo.set_setting(c, "send_time", send_time.zfill(5))
        repo.set_setting(c, "timezone", tz)
        repo.set_setting(c, "summary_threshold", str(threshold))
        repo.set_setting(c, "approaching_days", str(approaching))
        if old.get("send_time") != send_time.zfill(5) or old.get("timezone") != tz:
            # Let a new, later send time today still fire; never re-sends entries already notified today.
            c.execute("DELETE FROM settings WHERE key = 'last_run_day'")
    return redirect(request, "/settings", "Settings saved.")


@page("manage_settings")
async def settings_test(request: Request):
    ok, message = await request.app.state.service.send_test()
    return redirect(request, "/settings", message if ok else f"Test failed: {message}", "ok" if ok else "error")


# ---------------------------------------------------------------- notification setup helper
@page("view_notification_setup")
async def notify_setup(request: Request):
    cfg = request.app.state.cfg
    server = cfg.ntfy_public_url
    topic = cfg.ntfy_topic
    deep_link = qr_svg = None
    if server and topic:
        host = urlsplit(server).netloc
        deep_link = f"ntfy://{host}/{topic}"
        qr_svg = segno.make(deep_link, error="m").svg_inline(scale=5, dark="#111", light="#fff", border=2)
    return render(request, "notify_setup.html", server=server, topic=topic, deep_link=deep_link, qr_svg=qr_svg,
                  notifier=request.app.state.notifier.name)


def new_setup_code() -> str:
    return "-".join(secrets.token_hex(3) for _ in range(3))
