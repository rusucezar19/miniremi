"""Entry views (cards, grouped, timeline, table), entry CRUD, snooze, renew and CSV export."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import repo
from ..reminders import STATUSES
from ..util import add_years, fmt_date, parse_date, parse_duration
from .common import error_page, int_param, page, redirect, render, safe_next

VIEWS = ("cards", "grouped", "timeline", "table")
SNOOZE_PRESETS = [(1, "days", "1 day"), (3, "days", "3 days"), (1, "weeks", "1 week"), (2, "weeks", "2 weeks")]


def _ctx(request: Request):
    service = request.app.state.service
    with request.app.state.db.tx() as c:
        settings = repo.get_settings(c)
        columns = repo.get_columns(c)
        today = service.today()
        entries = repo.list_entries(c, today, int(settings.get("approaching_days", 30)), columns)
    return settings, columns, today, entries


def _filters(request: Request, columns):
    qp = request.query_params
    view = qp.get("view") or request.cookies.get("ct_view") or "cards"
    if view not in VIEWS:
        view = "cards"
    status = qp.get("status", "")
    if status not in STATUSES and status != "attention":
        status = ""
    center_col = repo.column_by_role(columns, "center")
    group_col = None
    try:
        group_id = int(qp.get("group", "") or 0)
        group_col = next((c for c in columns if c.id == group_id), None)
    except ValueError:
        pass
    group_col = group_col or center_col or (columns[0] if columns else None)
    try:
        months = max(1, min(24, int(qp.get("months", "6"))))
    except ValueError:
        months = 6
    return {
        "view": view,
        "q": qp.get("q", "").strip()[:100],
        "status": status,
        "center": qp.get("center", "").strip()[:200],
        "group_col": group_col,
        "center_col": center_col,
        "months": months,
    }


def _matches(e: repo.Entry, f, columns) -> bool:
    if f["status"] == "attention":
        if e.status not in ("due", "expired"):
            return False
    elif f["status"] and e.status != f["status"]:
        return False
    if f["center"] and f["center_col"] and e.display(f["center_col"]) != f["center"]:
        return False
    if f["q"]:
        needle = f["q"].lower()
        hay = " ".join(e.display(c) for c in columns).lower()
        if needle not in hay:
            return False
    return True


def _timeline(entries, today: date, months: int):
    start = today - timedelta(days=14)
    end = today + timedelta(days=int(months * 30.44))
    span = (end - start).days or 1

    def pct(d: date) -> float:
        return round(max(0.0, min(100.0, (d - start).days / span * 100)), 2)

    rows = []
    for e in entries:
        if not e.expiry:
            continue
        trig = e.trigger
        if trig > end:
            continue  # starts after the visible range (overdue entries stay visible, clipped left)
        left, right = pct(trig), pct(e.expiry)
        rows.append({"e": e, "left": left, "width": max(0.6, right - left), "marker": right,
                     "clipped_left": trig < start, "clipped_right": e.expiry > end})
    ticks = []
    m = date(start.year, start.month, 1)
    while m <= end:
        if m >= start:
            ticks.append({"label": m.strftime("%b %Y") if m.month == 1 or not ticks else m.strftime("%b"), "pct": pct(m)})
        m = date(m.year + (m.month // 12), m.month % 12 + 1, 1)
    hidden = sum(1 for e in entries if e.expiry and e.trigger > end)
    return {"rows": rows, "ticks": ticks, "today": pct(today), "start": start, "end": end,
            "hidden": hidden, "nodate": sum(1 for e in entries if not e.expiry)}


def _grouped(entries, col):
    groups: dict[str, list] = {}
    for e in entries:
        key = e.display(col) if col else ""
        groups.setdefault(key or "(none)", []).append(e)
    return sorted(groups.items(), key=lambda kv: (kv[0] == "(none)", kv[1][0].sort_key, kv[0].lower()))


@page("view_entries")
async def index(request: Request):
    settings, columns, today, entries = _ctx(request)
    f = _filters(request, columns)
    counts = {s: 0 for s in STATUSES}
    for e in entries:
        counts[e.status] += 1
    shown = [e for e in entries if _matches(e, f, columns)]
    center_options = []
    if f["center_col"]:
        seen = list(f["center_col"].options)
        for e in entries:
            v = e.display(f["center_col"])
            if v and v not in seen:
                seen.append(v)
        center_options = seen
    ctx = dict(
        columns=columns, entries=shown, total=len(entries), counts=counts, f=f, today=today,
        center_options=center_options, snooze_presets=SNOOZE_PRESETS,
        groupable=[c for c in columns if c.type in ("select", "text", "checkbox", "number", "date", "duration")],
        back=str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
    )
    if f["view"] == "grouped":
        ctx["groups"] = _grouped(shown, f["group_col"])
    if f["view"] == "timeline":
        ctx["tl"] = _timeline(shown, today, f["months"])
    resp = render(request, "index.html", **ctx)
    if request.query_params.get("view") in VIEWS:
        resp.set_cookie("ct_view", f["view"], max_age=365 * 86400, samesite="lax", httponly=True,
                        secure=request.app.state.cfg.cookie_secure)
    return resp


# ---------------------------------------------------------------- forms
def _form_values(form, columns) -> dict:
    values = {}
    for col in columns:
        name = f"c{col.id}"
        if col.type == "duration":
            values[col.key] = (form.get(f"{name}_n", ""), form.get(f"{name}_unit", "days"))
        elif col.type == "checkbox":
            values[col.key] = form.get(name) is not None
        else:
            values[col.key] = form.get(name, "")
    return values


def _entry_form_ctx(request, c, entry=None, data=None, error=None):
    columns = repo.get_columns(c)
    if data is None:
        data = dict(entry.data) if entry else {col.key: col.default for col in columns if col.default is not None}
    return dict(columns=columns, entry=entry, data=data, error=error)


@page("edit_entries")
async def new_entry(request: Request):
    db = request.app.state.db
    if request.method == "GET":
        with db.tx() as c:
            ctx = _entry_form_ctx(request, c)
        return render(request, "entry_form.html", **ctx)
    form = await request.form()
    with db.tx() as c:
        columns = repo.get_columns(c)
        try:
            data = repo.build_data(c, columns, _form_values(form, columns))
        except ValueError as exc:
            ctx = _entry_form_ctx(request, c, data=_raw_back(form, columns), error=str(exc))
            return render(request, "entry_form.html", status_code=400, raw=True, **ctx)
        new_id = repo.create_entry(c, data, request.state.user)
    return redirect(request, f"/entries/{new_id}", "Entry created.")


def _raw_back(form, columns) -> dict:
    """Echo submitted values back into the form after a validation error."""
    raw = {}
    for col in columns:
        name = f"c{col.id}"
        if col.type == "duration":
            raw[col.key] = {"n": form.get(f"{name}_n", ""), "unit": form.get(f"{name}_unit", "days")}
        elif col.type == "checkbox":
            raw[col.key] = form.get(name) is not None
        else:
            raw[col.key] = form.get(name, "")
    return raw


def _renew_presets(entry: repo.Entry):
    base = entry.expiry
    if not base:
        return []
    return [(n, fmt_date(add_years(base, n))) for n in (1, 2, 3)]


@page("view_entries")
async def entry_detail(request: Request):
    entry_id = int_param(request, "entry_id")
    db, service = request.app.state.db, request.app.state.service
    with db.tx() as c:
        settings = repo.get_settings(c)
        entry = repo.get_entry(c, entry_id, service.today(), int(settings.get("approaching_days", 30)))
        if entry is None:
            return error_page(request, 404, "Entry not found.")
        audit = repo.get_audit(c, entry_id)
        ctx = _entry_form_ctx(request, c, entry=entry)
    return render(request, "entry_detail.html", audit=audit, snooze_presets=SNOOZE_PRESETS,
                  renew_presets=_renew_presets(entry), back=safe_next(request.query_params.get("back")), **ctx)


@page("edit_entries")
async def update_entry(request: Request):
    entry_id = int_param(request, "entry_id")
    db = request.app.state.db
    form = await request.form()
    with db.tx() as c:
        row = c.execute("SELECT data FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            return error_page(request, 404, "Entry not found.")
        columns = repo.get_columns(c)
        try:
            data = repo.build_data(c, columns, _form_values(form, columns))
        except ValueError as exc:
            return redirect(request, f"/entries/{entry_id}", str(exc), "error")
        changes = repo.update_entry(c, entry_id, data, request.state.user)
    return redirect(request, f"/entries/{entry_id}", "Saved." if changes else "No changes.")


@page("edit_entries")
async def update_field(request: Request):
    """Inline edit from the table view: JSON {col, value}."""
    entry_id = int_param(request, "entry_id")
    db, service = request.app.state.db, request.app.state.service
    try:
        body = await request.json()
        col_id = int(body.get("col"))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)
    with db.tx() as c:
        settings = repo.get_settings(c)
        entry = repo.get_entry(c, entry_id, service.today(), int(settings.get("approaching_days", 30)))
        col = repo.get_column(c, col_id)
        if entry is None or col is None:
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
        try:
            data = repo.build_data(c, [col], {col.key: body.get("value")}, base=entry.data)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        repo.update_entry(c, entry_id, data, request.state.user)
        entry = repo.get_entry(c, entry_id, service.today(), int(settings.get("approaching_days", 30)))
    return JSONResponse({"ok": True, "display": entry.display(col), "status": entry.status, "status_label": entry.status_label})


@page("edit_entries")
async def delete_entry(request: Request):
    entry_id = int_param(request, "entry_id")
    db, service = request.app.state.db, request.app.state.service
    with db.tx() as c:
        entry = repo.get_entry(c, entry_id, service.today())
        if entry is None:
            return error_page(request, 404, "Entry not found.")
        if request.method == "GET":
            return render(request, "confirm.html", title="Delete entry",
                          message=f"Delete “{entry.title}”? This cannot be undone.",
                          action=f"/entries/{entry_id}/delete", button="Delete entry", cancel=f"/entries/{entry_id}")
        repo.delete_entry(c, entry_id, request.state.user)
    return redirect(request, "/", "Entry deleted.")


@page("edit_entries")
async def duplicate_entry(request: Request):
    entry_id = int_param(request, "entry_id")
    with request.app.state.db.tx() as c:
        try:
            new_id = repo.duplicate_entry(c, entry_id, request.state.user)
        except LookupError:
            return error_page(request, 404, "Entry not found.")
    return redirect(request, f"/entries/{new_id}", "Entry duplicated. You are now editing the copy.")


def _snooze_days(form) -> int:
    preset = form.get("preset")
    if preset:
        n, unit = preset.split(":", 1)
    else:
        n, unit = form.get("n", ""), form.get("unit", "days")
    dur = parse_duration(n, unit)
    days = dur["n"] * (7 if dur["unit"] == "weeks" else 1)
    if days < 1:
        raise ValueError("Snooze must be at least 1 day")
    return days


@page("edit_entries")
async def snooze(request: Request):
    entry_id = int_param(request, "entry_id")
    form = await request.form()
    back = safe_next(form.get("back"), f"/entries/{entry_id}")
    service = request.app.state.service
    try:
        days = _snooze_days(form)
        until = service.today() + timedelta(days=days)
        with request.app.state.db.tx() as c:
            repo.snooze_entry(c, entry_id, until, request.state.user)
    except ValueError as exc:
        return redirect(request, back, str(exc), "error")
    except LookupError:
        return error_page(request, 404, "Entry not found.")
    return redirect(request, back, f"Snoozed until {fmt_date(until)}.")


@page("edit_entries")
async def unsnooze(request: Request):
    entry_id = int_param(request, "entry_id")
    form = await request.form()
    with request.app.state.db.tx() as c:
        repo.unsnooze_entry(c, entry_id, request.state.user)
    return redirect(request, safe_next(form.get("back"), f"/entries/{entry_id}"), "Snooze cleared.")


@page("edit_entries")
async def bulk_snooze(request: Request):
    form = await request.form()
    back = safe_next(form.get("back"), "/")
    ids = []
    for raw in form.getlist("ids"):
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    if not ids:
        return redirect(request, back, "Select at least one entry.", "error")
    try:
        days = _snooze_days(form)
    except ValueError as exc:
        return redirect(request, back, str(exc), "error")
    until = request.app.state.service.today() + timedelta(days=days)
    done = 0
    with request.app.state.db.tx() as c:
        for entry_id in ids[:500]:
            try:
                repo.snooze_entry(c, entry_id, until, request.state.user, source="bulk")
                done += 1
            except LookupError:
                continue
    return redirect(request, back, f"Snoozed {done} entr{'y' if done == 1 else 'ies'} until {fmt_date(until)}.")


@page("edit_entries")
async def renew(request: Request):
    entry_id = int_param(request, "entry_id")
    form = await request.form()
    back = safe_next(form.get("back"), f"/entries/{entry_id}")
    try:
        new_expiry = parse_date(form.get("expiry", ""))
        if new_expiry is None:
            raise ValueError("A new expiry date is required")
        window = None
        if str(form.get("window_n", "")).strip():
            window = parse_duration(form.get("window_n"), form.get("window_unit", "days"))
        with request.app.state.db.tx() as c:
            repo.renew_entry(c, entry_id, new_expiry, window, request.state.user, request.app.state.service.today())
    except ValueError as exc:
        return redirect(request, back, str(exc), "error")
    except LookupError:
        return error_page(request, 404, "Entry not found.")
    return redirect(request, back, f"Marked as renewed. New expiry date {fmt_date(new_expiry)}.")


# ---------------------------------------------------------------- export
def _csv_safe(value: str) -> str:
    # Prevent spreadsheet formula injection.
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@page("export")
async def export_csv(request: Request):
    settings, columns, today, entries = _ctx(request)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([_csv_safe(c.name) for c in columns])
    for e in entries:
        writer.writerow([_csv_safe(e.display(c)) for c in columns])
    data = "﻿" + buf.getvalue()  # BOM so Excel detects UTF-8
    filename = f"certificates-{today.strftime('%d-%m-%Y')}.csv"
    return Response(data.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
