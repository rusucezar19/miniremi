"""Login, logout, first-run setup and own-password change."""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import RedirectResponse

from .. import repo, security
from .common import error_page, page, redirect, render, safe_next

log = logging.getLogger("certtracker.auth")


def _session_cookie(request: Request, response, token: str):
    cfg = request.app.state.cfg
    response.set_cookie(
        security.SESSION_COOKIE, token, max_age=cfg.session_days * 86400,
        httponly=True, secure=cfg.cookie_secure, samesite="lax", path="/",
    )


async def _prelogin_csrf_ok(request: Request) -> bool:
    form = await request.form()
    return security.tokens_equal(form.get("csrf"), request.cookies.get(security.PRELOGIN_COOKIE))


# ---------------------------------------------------------------- setup
async def setup(request: Request):
    db, cfg = request.app.state.db, request.app.state.cfg
    with db.tx() as c:
        if repo.count_users(c) > 0:
            return RedirectResponse("/login", status_code=303)
    if request.method == "GET":
        return render(request, "setup.html", min_len=cfg.min_password_length)
    if not await _prelogin_csrf_ok(request):
        return error_page(request, 403, "Form expired. Reload the page and try again.")
    form = await request.form()
    code_expected = request.app.state.setup_code
    error = None
    if not code_expected or not security.tokens_equal(str(form.get("setup_code", "")).strip(), code_expected):
        error = "The setup code is wrong. Read it from data/setup-code.txt on the server."
    else:
        try:
            security.validate_new_password(form.get("password", ""), cfg.min_password_length, form.get("confirm", ""))
            with db.tx() as c:
                if repo.count_users(c) > 0:
                    return RedirectResponse("/login", status_code=303)
                repo.create_user(c, form.get("username", ""), security.hash_password(form["password"]), is_admin=True)
        except ValueError as exc:
            error = str(exc)
    if error:
        return render(request, "setup.html", status_code=400, error=error, username=form.get("username", ""), min_len=cfg.min_password_length)
    request.app.state.clear_setup_code()
    log.info("Admin account created via setup screen")
    return redirect(request, "/login", "Admin account created. Please log in.")


# ---------------------------------------------------------------- login / logout
async def login(request: Request):
    db, cfg = request.app.state.db, request.app.state.cfg
    with db.tx() as c:
        if repo.count_users(c) == 0:
            return RedirectResponse("/setup", status_code=303)
    nxt = safe_next(request.query_params.get("next"))
    if request.method == "GET":
        if request.state.user:
            return RedirectResponse(nxt, status_code=303)
        return render(request, "login.html", next=nxt)
    if not await _prelogin_csrf_ok(request):
        return error_page(request, 403, "Form expired. Reload the page and try again.")
    form = await request.form()
    nxt = safe_next(form.get("next"))
    username = str(form.get("username", ""))[:100]
    limiter = request.app.state.ip_limiter
    ip = request.state.client_ip
    if limiter.blocked(ip):
        return render(request, "login.html", status_code=429, error="Too many login attempts from your network. Try again later.", next=nxt, username=username)
    with db.tx() as c:
        user, error = security.check_login(
            c, username, str(form.get("password", "")), cfg.login_max_failures, cfg.login_lockout_minutes
        )
        token = security.create_session(c, user["id"], cfg.session_days) if user else None
    if not user:
        limiter.fail(ip)
        log.info("Failed login for a user from %s", ip)
        return render(request, "login.html", status_code=401, error=error, next=nxt, username=username)
    limiter.reset(ip)
    resp = RedirectResponse(nxt, status_code=303)
    _session_cookie(request, resp, token)
    resp.delete_cookie(security.PRELOGIN_COOKIE, path="/")
    return resp


@page(permission=None)
async def logout(request: Request):
    with request.app.state.db.tx() as c:
        security.delete_session(c, request.cookies.get(security.SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(security.SESSION_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- account
@page("change_own_password")
async def account(request: Request):
    return render(request, "account.html", min_len=request.app.state.cfg.min_password_length)


@page("change_own_password")
async def change_password(request: Request):
    cfg, db = request.app.state.cfg, request.app.state.db
    form = await request.form()
    user = request.state.user
    try:
        if not security.verify_password(user["password_hash"], str(form.get("current", ""))):
            raise ValueError("Your current password is wrong")
        new = str(form.get("password", ""))
        security.validate_new_password(new, cfg.min_password_length, str(form.get("confirm", "")))
        if new == form.get("current"):
            raise ValueError("Choose a password different from the current one")
    except ValueError as exc:
        return render(request, "account.html", status_code=400, error=str(exc), min_len=cfg.min_password_length)
    with db.tx() as c:
        c.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (security.hash_password(new), user["id"]),
        )
        security.delete_user_sessions(c, user["id"], keep_token=request.cookies.get(security.SESSION_COOKIE))
    return redirect(request, "/", "Password changed. Other sessions were logged out.")
