"""AgentMail: the email plumbing for the per-transaction threads (SPEC §5).

A thin, deterministic client — sending and replying are direct API calls, not
agentic. One inbox is the agent's address; each transaction is one thread within
it (started by the first send, continued by replies). The model composes the
prose elsewhere; this module only puts it on the wire.

The backend is a small :class:`MailBackend` protocol over *our* two operations
(send a new thread / reply on one), so tests inject a fake and strict mypy never
has to reason about the AgentMail SDK's surface. :func:`MailClient.from_env`
wires the real SDK behind that protocol, reading ``AGENTMAIL_API_KEY``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from pydantic import model_validator

from ynab_agent.domain.base import Frozen

if TYPE_CHECKING:
    from agentmail import AgentMail

_API_KEY_ENV = "AGENTMAIL_API_KEY"


class SentEmail(Frozen):
    """The identifiers AgentMail returns for a sent message."""

    message_id: str
    thread_id: str


class OutboundEmail(Frozen):
    """One message to send: a new thread, or a reply continuing one."""

    inbox_id: str
    subject: str
    text: str
    to: tuple[str, ...] = ()
    reply_to_message_id: str | None = None

    @model_validator(mode="after")
    def _check_addressing(self) -> OutboundEmail:
        if self.reply_to_message_id is None and not self.to:
            msg = "a new-thread email needs at least one recipient"
            raise ValueError(msg)
        return self


class MailBackend(Protocol):
    """The two mail operations the client needs (implemented over AgentMail)."""

    def send_new(
        self, inbox_id: str, to: list[str], subject: str, text: str
    ) -> SentEmail:
        """Start a new thread."""
        ...

    def send_reply(
        self, inbox_id: str, message_id: str, text: str
    ) -> SentEmail:
        """Reply on an existing thread."""
        ...


# The process-wide cached client built by ``from_env`` (see its docstring).
# ``tests/conftest.py`` resets this between tests.
_CACHED: MailClient | None = None


class MailClient:
    """Sends a transaction's outbound mail through AgentMail (SPEC §5)."""

    def __init__(self, backend: MailBackend) -> None:
        """Wrap a backend (the real AgentMail adapter, or a test fake)."""
        self._backend = backend

    @classmethod
    def from_env(cls) -> MailClient:
        """Build (once) a client backed by the real AgentMail SDK.

        Cached like the YNAB client: the instrumented httpx client is built once
        and reused, so a per-call client (never closed) can't leak sockets.
        Tests reset the cache (see ``tests/conftest.py``).

        Raises:
            RuntimeError: If ``AGENTMAIL_API_KEY`` is not set.
        """
        global _CACHED
        if _CACHED is not None:
            return _CACHED
        key = os.environ.get(_API_KEY_ENV)
        if not key:
            msg = f"{_API_KEY_ENV} is not set"
            raise RuntimeError(msg)
        import httpx
        from agentmail import AgentMail

        from ynab_agent.telemetry import instrument_httpx

        # Inject our own httpx client so trace context propagates on mail sends
        # (the SDK accepts one); instrument it the same way the YNAB client is.
        http_client = httpx.Client(timeout=60.0)
        instrument_httpx(http_client)
        _CACHED = cls(
            _AgentMailBackend(AgentMail(api_key=key, httpx_client=http_client))
        )
        return _CACHED

    def send(self, email: OutboundEmail) -> SentEmail:
        """Send a new thread, or a reply if a message to reply to is set."""
        if email.reply_to_message_id is not None:
            return self._backend.send_reply(
                email.inbox_id, email.reply_to_message_id, email.text
            )
        return self._backend.send_new(
            email.inbox_id, list(email.to), email.subject, email.text
        )


class _AgentMailBackend:
    """Adapts the AgentMail SDK to the :class:`MailBackend` protocol."""

    def __init__(self, client: AgentMail) -> None:
        self._client = client

    def send_new(
        self, inbox_id: str, to: list[str], subject: str, text: str
    ) -> SentEmail:
        result = self._client.inboxes.messages.send(
            inbox_id, to=to, subject=subject, text=text
        )
        return SentEmail(
            message_id=result.message_id, thread_id=result.thread_id
        )

    def send_reply(
        self, inbox_id: str, message_id: str, text: str
    ) -> SentEmail:
        result = self._client.inboxes.messages.reply(
            inbox_id, message_id, text=text
        )
        return SentEmail(
            message_id=result.message_id, thread_id=result.thread_id
        )
