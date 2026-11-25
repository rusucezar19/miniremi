from __future__ import annotations

from .base import Action, Notification, Notifier, NotifyError
from .console import ConsoleNotifier
from .ntfy import NtfyNotifier


def make_notifier(cfg) -> Notifier:
    if cfg.notifier == "ntfy":
        return NtfyNotifier(cfg.ntfy_url, cfg.ntfy_topic, cfg.ntfy_token)
    return ConsoleNotifier()


__all__ = ["Action", "Notification", "Notifier", "NotifyError", "ConsoleNotifier", "NtfyNotifier", "make_notifier"]
