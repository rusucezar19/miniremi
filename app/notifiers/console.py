from __future__ import annotations

import logging
from collections import deque

from .base import Notification, Notifier

log = logging.getLogger("certtracker.notify.console")


class ConsoleNotifier(Notifier):
    """Prints notifications to the log. For local development and tests."""

    name = "console"

    def __init__(self, keep: int = 200):
        self.sent: deque[Notification] = deque(maxlen=keep)

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)
        labels = ", ".join(a.label for a in notification.actions)
        log.info(
            "NOTIFY [p%s] %s | %s | click=%s | actions=[%s]",
            notification.priority,
            notification.title,
            notification.message.replace("\n", " / "),
            notification.click,
            labels,
        )
