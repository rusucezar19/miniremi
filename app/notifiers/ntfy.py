from __future__ import annotations

import httpx

from .base import Notification, Notifier, NotifyError


class NtfyNotifier(Notifier):
    """Publishes JSON messages to a (self-hosted) ntfy server."""

    name = "ntfy"

    def __init__(self, url: str, topic: str, token: str = "", transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0):
        if not url or not topic:
            raise ValueError("NTFY_URL and NTFY_TOPIC are required for the ntfy notifier")
        self.url = url.rstrip("/") + "/"
        self.topic = topic
        self._token = token
        self._transport = transport
        self._timeout = timeout

    def payload(self, n: Notification) -> dict:
        body: dict = {
            "topic": self.topic,
            "title": n.title,
            "message": n.message,
            "priority": n.priority,
        }
        if n.tags:
            body["tags"] = n.tags
        if n.click:
            body["click"] = n.click
        actions = []
        for a in n.actions[:3]:  # ntfy allows up to three actions
            if a.kind == "http":
                actions.append({
                    "action": "http",
                    "label": a.label,
                    "url": a.url,
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": a.body or "",
                    "clear": a.clear,
                })
            else:
                actions.append({"action": "view", "label": a.label, "url": a.url, "clear": a.clear})
        if actions:
            body["actions"] = actions
        return body

    async def send(self, notification: Notification) -> None:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                resp = await client.post(self.url, json=self.payload(notification), headers=headers)
        except httpx.HTTPError as exc:
            raise NotifyError(f"Could not reach ntfy at {self.url}: {type(exc).__name__}: {exc}") from None
        if resp.status_code >= 300:
            detail = resp.text.strip().replace("\n", " ")[:300]
            raise NotifyError(f"ntfy returned HTTP {resp.status_code}: {detail}")
