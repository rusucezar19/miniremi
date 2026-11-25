"""Application factory: wiring, middleware, routes and lifespan."""
from __future__ import annotations

import contextlib
import logging
import secrets
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import repo, security
from .config import Config
from .db import Database
from .notifiers import Notifier, make_notifier
from .routes import actions, admin, auth, entries
from .routes.common import client_ip, is_https
from .scheduler import ReminderService

log = logging.getLogger("certtracker")
access_log = logging.getLogger("certtracker.access")
STATIC_DIR = Path(__file__).resolve().parent / "static"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; manifest-src 'self'"
)


class AppMiddleware(BaseHTTPMiddleware):
    """Proxy-aware client info, session loading, security headers and a token-free access log."""

    async def dispatch(self, request: Request, call_next):
        state = request.app.state
        cfg: Config = state.cfg
        started = time.monotonic()
        path = request.url.path
        request.state.client_ip = client_ip(request, cfg.trust_proxy)
        request.state.https = is_https(request, cfg.trust_proxy)
        request.state.session = request.state.user = None
        new_pre = None
        is_static = path.startswith("/static/") or path in ("/healthz", "/sw.js", "/manifest.webmanifest")
        if not is_static:
            token = request.cookies.get(security.SESSION_COOKIE)
            if token:
                with state.db.tx() as c:
                    session, user = security.load_session(c, token)
                request.state.session, request.state.user = session, user
            pre = request.cookies.get(security.PRELOGIN_COOKIE)
            if not pre:
                pre = new_pre = secrets.token_urlsafe(24)
            request.state.pre_csrf = pre

        response = await call_next(request)

        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.state.https:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not is_static:
            h.setdefault("Cache-Control", "no-store")
        if new_pre:
            response.set_cookie(security.PRELOGIN_COOKIE, new_pre, max_age=12 * 3600, httponly=True,
                                secure=cfg.cookie_secure, samesite="strict", path="/")
        if path != "/healthz" and not path.startswith("/static/"):
            # Path only: no query strings, no bodies (action tokens travel in bodies).
            access_log.info("%s %s %s %.0fms %s", request.method, path, response.status_code,
                            (time.monotonic() - started) * 1000, request.state.client_ip)
        return response


async def healthz(request: Request):
    with request.app.state.db.tx() as c:
        c.execute("SELECT 1").fetchone()
    return JSONResponse({"ok": True})


async def service_worker(request: Request):
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


async def manifest(request: Request):
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


def routes() -> list:
    R = Route
    return [
        R("/", entries.index),
        R("/healthz", healthz),
        R("/sw.js", service_worker),
        R("/manifest.webmanifest", manifest),
        R("/setup", auth.setup, methods=["GET", "POST"]),
        R("/login", auth.login, methods=["GET", "POST"]),
        R("/logout", auth.logout, methods=["POST"]),
        R("/account", auth.account),
        R("/account/password", auth.change_password, methods=["POST"]),
        R("/entries/new", entries.new_entry, methods=["GET", "POST"]),
        R("/entries/bulk-snooze", entries.bulk_snooze, methods=["POST"]),
        R("/entries/{entry_id:int}", entries.entry_detail),
        R("/entries/{entry_id:int}/edit", entries.update_entry, methods=["POST"]),
        R("/entries/{entry_id:int}/field", entries.update_field, methods=["POST"]),
        R("/entries/{entry_id:int}/delete", entries.delete_entry, methods=["GET", "POST"]),
        R("/entries/{entry_id:int}/duplicate", entries.duplicate_entry, methods=["POST"]),
        R("/entries/{entry_id:int}/snooze", entries.snooze, methods=["POST"]),
        R("/entries/{entry_id:int}/unsnooze", entries.unsnooze, methods=["POST"]),
        R("/entries/{entry_id:int}/renew", entries.renew, methods=["POST"]),
        R("/export.csv", entries.export_csv),
        R("/import", entries.import_entries, methods=["GET", "POST"]),
        R("/import/template.csv", entries.import_template),
        R("/columns", admin.columns_page),
        R("/columns/add", admin.column_add, methods=["POST"]),
        R("/columns/{column_id:int}", admin.column_update, methods=["POST"]),
        R("/columns/{column_id:int}/type", admin.column_type, methods=["POST"]),
        R("/columns/{column_id:int}/delete", admin.column_delete, methods=["GET", "POST"]),
        R("/users", admin.users_page),
        R("/users/add", admin.user_add, methods=["POST"]),
        R("/users/{user_id:int}", admin.user_update, methods=["POST"]),
        R("/settings", admin.settings_page),
        R("/settings/save", admin.settings_save, methods=["POST"]),
        R("/settings/test", admin.settings_test, methods=["POST"]),
        R("/notifications", admin.notify_setup),
        R("/notifications/test", admin.notify_test, methods=["POST"]),
        R("/api/entries/{entry_id:int}/action/{action}", actions.notification_action, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]


def bootstrap_admin(db: Database, cfg: Config) -> bool:
    """Create the admin from ADMIN_USERNAME / ADMIN_PASSWORD if there are no users. Returns True if users exist."""
    with db.tx() as c:
        if repo.count_users(c) > 0:
            return True
        if cfg.admin_username and cfg.admin_password:
            try:
                security.validate_new_password(cfg.admin_password, cfg.min_password_length)
                repo.create_user(c, cfg.admin_username, security.hash_password(cfg.admin_password), is_admin=True)
                log.info("Created admin account '%s' from environment variables", cfg.admin_username)
                return True
            except ValueError as exc:
                log.error("ADMIN_USERNAME/ADMIN_PASSWORD rejected (%s); use the setup screen instead", exc)
    return False


def create_app(cfg: Config | None = None, notifier: Notifier | None = None, start_scheduler: bool = True) -> Starlette:
    cfg = cfg or Config.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.ensure_secret()
    db = Database(cfg.db_path)
    db.init(cfg.tz)
    notifier = notifier or make_notifier(cfg)
    service = ReminderService(db, notifier, cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        if start_scheduler:
            service.start()  # first tick runs immediately: catch-up after downtime
        log.info("Certificate Tracker ready (notifier=%s, public URL=%s)", notifier.name, cfg.public_base_url)
        try:
            yield
        finally:
            await service.stop()
            await notifier.aclose()

    app = Starlette(routes=routes(), middleware=[Middleware(AppMiddleware)], lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.notifier = notifier
    app.state.service = service
    app.state.ip_limiter = security.IPLimiter(cfg.ip_max_failures, cfg.ip_window_minutes)

    setup_file = cfg.data_dir / "setup-code.txt"

    def clear_setup_code():
        app.state.setup_code = None
        with contextlib.suppress(OSError):
            setup_file.unlink()

    app.state.clear_setup_code = clear_setup_code
    app.state.setup_code = None
    if bootstrap_admin(db, cfg):
        clear_setup_code()
    else:
        code = admin.new_setup_code()
        setup_file.write_text(code + "\n")
        with contextlib.suppress(OSError):
            setup_file.chmod(0o600)
        app.state.setup_code = code
        log.warning("No admin account yet. Open %s/setup and enter the code stored in %s", cfg.public_base_url, setup_file)
    return app
