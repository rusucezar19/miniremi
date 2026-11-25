"""Notifier interface. To add a channel (Web Push, email), add one subclass."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class NotifyError(Exception):
    """Raised when a notification could not be delivered. The message is shown to admins."""


@dataclass
class Action:
    kind: str  # "http" (POST to url with body) or "view" (open url)
    label: str
    url: str
    body: str | None = None  # secret (action token) for http actions; never log it
    clear: bool = False

    def __repr__(self) -> str:  # keep tokens out of logs and tracebacks
        return f"Action(kind={self.kind!r}, label={self.label!r}, url={self.url!r})"


@dataclass
class Notification:
    title: str
    message: str
    priority: int = 3  # 1..5, ntfy scale; 3 = default, 4 = high
    click: str | None = None
    actions: list[Action] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


class Notifier(ABC):
    name = "base"

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """Deliver or raise NotifyError."""

    async def aclose(self) -> None:
        return None
