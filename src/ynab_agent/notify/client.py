"""ntfy: a push to the owner's phone for operational alerts (SPEC §13).

A thin, deterministic client — an alert is a single HTTP POST to a topic on an
ntfy server (https://ntfy.sh by default). The topic name *is* the secret: anyone
who knows it can read and publish, so it is read from the environment
(``NTFY_TOPIC``), never the repo, exactly like the YNAB/AgentMail keys.

The backend is a small :class:`NotifyBackend` protocol over our one operation
(publish a notification), so tests inject a fake and strict mypy never reasons
about httpx. :func:`NotifyClient.from_env` wires the real POST behind it.

Robustness note: callers send alerts from the *failure-handling* path, so the
client must be cheap and predictable, but it is the *activity* layer
(``workflow.alert_activities``) that swallows errors — a failed alert must never
mask the original failure. This module raises normally so it stays testable.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal, Protocol

from ynab_agent.domain.base import Frozen

if TYPE_CHECKING:
    import httpx

_TOPIC_ENV = "NTFY_TOPIC"
_BASE_URL_ENV = "NTFY_BASE_URL"
_TOKEN_ENV = "NTFY_TOKEN"
_DEFAULT_BASE_URL = "https://ntfy.sh"
# A short, fixed ceiling: an alert that can't post in a few seconds is not worth
# blocking a failing transaction's teardown on.
_HTTP_TIMEOUT_S = 10.0

# ntfy's five priority levels (https://docs.ntfy.sh/publish/#message-priority).
Priority = Literal["min", "low", "default", "high", "urgent"]


class Notification(Frozen):
    """One push: a title line, a body, a priority, and tag emojis."""

    title: str
    body: str
    priority: Priority = "high"
    tags: tuple[str, ...] = ()


class NotifyBackend(Protocol):
    """The one operation the client needs (implemented over ntfy's HTTP API)."""

    def publish(self, notification: Notification) -> None:
        """Send one notification to the configured topic."""
        ...


# The process-wide cached client built by ``from_env``; ``tests/conftest.py``
# resets it between tests (like the YNAB/AgentMail clients).
_CACHED: NotifyClient | None = None


class NotifyClient:
    """Pushes operational alerts to the owner's ntfy topic (SPEC §13)."""

    def __init__(self, backend: NotifyBackend) -> None:
        """Wrap a backend (the real httpx adapter, or a test fake)."""
        self._backend = backend

    @classmethod
    def from_env(cls) -> NotifyClient:
        """Build (once) a client posting to ``NTFY_TOPIC``.

        Reads ``NTFY_TOPIC`` (required), ``NTFY_BASE_URL`` (default
        ``https://ntfy.sh``) and ``NTFY_TOKEN`` (optional, for a protected
        topic). Cached like the other clients; tests reset the cache.

        Raises:
            RuntimeError: If ``NTFY_TOPIC`` is not set. Callers in the alert
                path catch this and degrade to a logged no-op, so a worker with
                alerting unconfigured still runs — it just cannot page.
        """
        global _CACHED
        if _CACHED is not None:
            return _CACHED
        topic = os.environ.get(_TOPIC_ENV)
        if not topic:
            msg = f"{_TOPIC_ENV} is not set"
            raise RuntimeError(msg)
        base_url = os.environ.get(_BASE_URL_ENV) or _DEFAULT_BASE_URL
        token = os.environ.get(_TOKEN_ENV) or None
        import httpx

        http_client = httpx.Client(timeout=_HTTP_TIMEOUT_S)
        _CACHED = cls(
            _HttpxBackend(
                http_client, base_url=base_url, topic=topic, token=token
            )
        )
        return _CACHED

    def notify(self, notification: Notification) -> None:
        """Publish one notification (may raise; the activity layer guards)."""
        self._backend.publish(notification)


class _HttpxBackend:
    """Posts a :class:`Notification` to ``{base_url}/{topic}`` over httpx."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        base_url: str,
        topic: str,
        token: str | None,
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/{topic}"
        self._token = token

    def publish(self, notification: Notification) -> None:
        # ntfy reads the title/priority/tags from headers and the body from the
        # request data (https://docs.ntfy.sh/publish/).
        headers = {
            "Title": notification.title,
            "Priority": notification.priority,
        }
        if notification.tags:
            headers["Tags"] = ",".join(notification.tags)
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        response = self._client.post(
            self._url,
            content=notification.body.encode("utf-8"),
            headers=headers,
        )
        response.raise_for_status()
