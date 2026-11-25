"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("certtracker.config")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Config:
    data_dir: Path = Path("/data")
    port: int = 8080
    tz: str = "Europe/Bucharest"
    secret_key: str = ""
    trust_proxy: bool = False
    public_base_url: str = "http://localhost:8080"
    admin_username: str | None = None
    admin_password: str | None = None
    notifier: str = "console"  # "ntfy" or "console"
    ntfy_url: str = ""
    ntfy_public_url: str = ""
    ntfy_topic: str = ""
    ntfy_token: str = ""
    min_password_length: int = 10
    session_days: int = 30
    cookie_secure: bool = False
    # Delays (seconds) between delivery attempts of one notification.
    retry_delays: tuple[float, ...] = (2.0, 10.0, 30.0)
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    ip_max_failures: int = 20
    ip_window_minutes: int = 15
    scheduler_interval_seconds: int = 30
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "certtracker.db"

    def topic_for(self, username: str) -> str:
        """Personal ntfy topic of a user: <NTFY_TOPIC>-<username>.

        ntfy topics allow only letters, digits, '-' and '_'. Usernames with other characters
        get a short hash suffix so two users can never end up on the same topic.
        """
        name = (username or "").strip().lower()
        slug = re.sub(r"[^a-z0-9_-]", "_", name)
        if slug != name:
            slug += "-" + hashlib.sha256(name.encode()).hexdigest()[:6]
        base = self.ntfy_topic or "certs"
        return f"{base}-{slug}"[:64]

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        data_dir = Path(env.get("DATA_DIR") or "/data")
        public = (env.get("PUBLIC_BASE_URL") or f"http://localhost:{_int(env.get('PORT'), 8080)}").rstrip("/")
        ntfy_url = (env.get("NTFY_URL") or "").rstrip("/")
        ntfy_topic = env.get("NTFY_TOPIC") or ""
        notifier = (env.get("NOTIFIER") or "").strip().lower()
        if notifier not in ("ntfy", "console"):
            notifier = "ntfy" if ntfy_url and ntfy_topic else "console"
        cfg = cls(
            data_dir=data_dir,
            port=_int(env.get("PORT"), 8080),
            tz=env.get("TZ") or "Europe/Bucharest",
            secret_key=env.get("SECRET_KEY") or "",
            trust_proxy=_bool(env.get("TRUST_PROXY")),
            public_base_url=public,
            admin_username=(env.get("ADMIN_USERNAME") or "").strip() or None,
            admin_password=env.get("ADMIN_PASSWORD") or None,
            notifier=notifier,
            ntfy_url=ntfy_url,
            ntfy_public_url=(env.get("NTFY_PUBLIC_URL") or "").rstrip("/"),
            ntfy_topic=ntfy_topic,
            ntfy_token=env.get("NTFY_TOKEN") or "",
            min_password_length=max(8, _int(env.get("MIN_PASSWORD_LENGTH"), 10)),
            session_days=max(1, _int(env.get("SESSION_DAYS"), 30)),
            cookie_secure=_bool(env.get("COOKIE_SECURE"), default=public.startswith("https://")),
        )
        return cfg

    def ensure_secret(self) -> None:
        """Use SECRET_KEY, or generate one and keep it in the data volume."""
        if self.secret_key:
            if len(self.secret_key) < 32:
                log.warning("SECRET_KEY is shorter than 32 characters; use a longer random value.")
            return
        path = self.data_dir / ".secret_key"
        if path.exists():
            self.secret_key = path.read_text().strip()
        else:
            self.secret_key = secrets.token_urlsafe(48)
            path.write_text(self.secret_key)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        log.warning("SECRET_KEY not set; using a generated key stored in the data volume.")
