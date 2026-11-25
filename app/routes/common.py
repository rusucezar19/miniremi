"""Shared web helpers: rendering, auth decorator, CSRF, flash messages, proxy-aware client info."""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from .. import permissions, repo
from ..reminders import STATUS_LABELS, STATUSES
from ..security import tokens_equal
from ..util import DATE_FORMAT_HINT, fmt_date, fmt_duration

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(
    fmt_date=fmt_date,
    fmt_duration=fmt_duration,
    can=permissions.can,
    STATUSES=STATUSES,
    STATUS_LABELS=STATUS_LABELS,
    COLUMN_TYPES=repo.COLUMN_TYPES,
    DATE_HINT=DATE_FORMAT_HINT,
)

FLASH_COOKIE = "ct_flash"


# ---------------------------------------------------------------- proxy awareness
def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        xff = request.headers.get("x-forwarded-for", "")
        # The right-most entry was added by our own reverse proxy (Tailscale), so trust only that one.
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def is_https(request: Request, trust_proxy: bool) -> bool:
    if trust_proxy:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower()
        if proto:
            return proto == "https"
    return request.url.scheme == "https"


# ---------------------------------------------------------------- flash messages
def _flash_sig(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), b"flash:" + payload.encode(), hashlib.sha256).hexdigest()[:32]


def set_flash(response, request: Request, text: str, kind: str = "ok"):
    cfg = request.app.state.cfg
    payload = base64.urlsafe_b64encode(json.dumps({"k": kind, "t": text[:500]}).encode()).decode()
    response.set_cookie(
        FLASH_COOKIE, f"{payload}.{_flash_sig(cfg.secret_key, payload)}",
        max_age=60, httponly=True, secure=cfg.cookie_secure, samesite="lax", path="/",
    )
    return response


def read_flash(request: Request):
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw or "." not in raw:
        return None
    payload, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _flash_sig(request.app.state.cfg.secret_key, payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return {"kind": data.get("k", "ok"), "text": str(data.get("t", ""))}
    except ValueError:
        return None


def redirect(request: Request, url: str, flash: str | None = None, kind: str = "ok"):
    resp = RedirectResponse(url, status_code=303)
    if flash:
        set_flash(resp, request, flash, kind)
    return resp


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow local redirects."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default
    return url


# ---------------------------------------------------------------- rendering
def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    session = getattr(request.state, "session", None)
    flash = read_flash(request)
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("csrf_token", session["csrf"] if session else getattr(request.state, "pre_csrf", ""))
    ctx.setdefault("flash", flash)
    ctx.setdefault("public_base_url", request.app.state.cfg.public_base_url)
    resp = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    if flash:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def error_page(request: Request, status: int, message: str):
    return render(request, "error.html", status_code=status, status=status, message=message)


# ---------------------------------------------------------------- CSRF
def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return True  # token check below still applies
    host = urlsplit(origin).netloc.lower()
    allowed = {request.headers.get("host", "").lower(), urlsplit(request.app.state.cfg.public_base_url).netloc.lower()}
    if request.app.state.cfg.trust_proxy and request.headers.get("x-forwarded-host"):
        allowed.add(request.headers["x-forwarded-host"].split(",")[-1].strip().lower())
    return host in allowed


async def csrf_valid(request: Request, expected: str | None) -> bool:
    if not _origin_ok(request):
        return False
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if "form" in ctype:
            form = await request.form()
            supplied = form.get("csrf")
    return tokens_equal(supplied, expected)


# ---------------------------------------------------------------- auth decorator
def page(permission: str | None = "view_entries", login: bool = True):
    """Wrap an endpoint: require login, check permission, verify CSRF on unsafe methods."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(request: Request):
            user = getattr(request.state, "user", None)
            if login and user is None:
                if request.method == "GET":
                    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
                return PlainTextResponse("Login required", status_code=401)
            if user is not None and user["must_change_password"] and request.url.path not in ("/account", "/account/password", "/logout"):
                if request.method == "GET":
                    return redirect(request, "/account", "Please choose a new password first.", "warn")
                return PlainTextResponse("Password change required", status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                session = getattr(request.state, "session", None)
                if not await csrf_valid(request, session["csrf"] if session else None):
                    return error_page(request, 403, "Your session expired or the form was tampered with. Reload the page and try again.")
            if permission and login and not permissions.can(user, permission):
                return error_page(request, 403, "You do not have permission to do that.")
            return await fn(request)

        return wrapper

    return deco


def int_param(request: Request, name: str) -> int:
    try:
        return int(request.path_params[name])
    except (KeyError, ValueError):
        return -1
