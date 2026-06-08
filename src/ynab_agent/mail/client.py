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
# Every agent message carries this label, plus per-transaction / per-action
# labels that serve as idempotency keys (no separate store — derived from the
# AgentMail thread, SPEC §5).
_AGENT_LABEL = "ynab-agent"


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
    """The mail operations the client needs (implemented over AgentMail)."""

    def send_new(
        self,
        inbox_id: str,
        to: list[str],
        subject: str,
        text: str,
        labels: list[str] | None = None,
    ) -> SentEmail:
        """Start a new thread."""
        ...

    def send_reply(
        self,
        inbox_id: str,
        message_id: str,
        text: str,
        labels: list[str] | None = None,
        to: list[str] | None = None,
    ) -> SentEmail:
        """Reply on an existing thread.

        ``to`` overrides the recipients. AgentMail otherwise addresses a reply
        to the *sender* of ``message_id``; when that message is the agent's own
        (replying on a thread the agent last spoke on), the reply loops back to
        the agent and the owner never sees it. Pass the owners to deliver it.
        """
        ...

    def find_thread(self, inbox_id: str, label: str) -> str | None:
        """The thread id of the first message carrying ``label``, or None.

        Labels are the agent's idempotency keys: a per-transaction label lets
        ``open_thread`` find an already-opened thread instead of duplicating it.
        """
        ...

    def latest_message_id(self, inbox_id: str, thread_id: str) -> str | None:
        """The newest message id on a thread (the target to reply to)."""
        ...

    def archive(self, inbox_id: str, thread_id: str) -> None:
        """Mark a thread closed (an ``ynab-archived`` label)."""
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
                email.inbox_id,
                email.reply_to_message_id,
                email.text,
                to=list(email.to) or None,
            )
        return self._backend.send_new(
            email.inbox_id, list(email.to), email.subject, email.text
        )

    def open_thread(
        self,
        *,
        inbox_id: str,
        to: list[str],
        subject: str,
        body: str,
        txn_label: str,
    ) -> str:
        """Open the transaction's thread (idempotent); return its id.

        If a thread already carries ``txn_label`` (a retry of this open), return
        it rather than starting a duplicate; otherwise send the opening message.
        """
        existing = self._backend.find_thread(inbox_id, txn_label)
        if existing is not None:
            return existing
        sent = self._backend.send_new(
            inbox_id, to, subject, body, labels=[_AGENT_LABEL, txn_label]
        )
        return sent.thread_id

    def send_on_thread(
        self,
        *,
        inbox_id: str,
        thread_id: str,
        body: str,
        seq_label: str,
        to: list[str] | None = None,
    ) -> bool:
        """Reply on a thread (idempotent on ``seq_label``); True if sent.

        Skips if a message with ``seq_label`` is already on record, so a retry
        never double-sends (SPEC §3 outbound dedup). ``to`` overrides the
        recipients: when the thread's latest message is the agent's own (e.g.
        the W6 alert the balancer replies on), AgentMail would address the reply
        back to the agent, so pass the owners to deliver it to them (SPEC §8).
        """
        if self._backend.find_thread(inbox_id, seq_label) is not None:
            return False
        target = self._backend.latest_message_id(inbox_id, thread_id)
        if target is None:
            return False
        self._backend.send_reply(
            inbox_id, target, body, labels=[_AGENT_LABEL, seq_label], to=to
        )
        return True

    def alert_on_thread(
        self,
        *,
        inbox_id: str,
        to: list[str],
        subject: str,
        body: str,
        thread_label: str,
        update_label: str,
    ) -> str:
        """Open the alert thread, or reply a worsening update on it (SPEC §7).

        One thread per overspend, keyed by the stable ``thread_label``. The
        first alert opens it (``body`` as the opening message), tagged with both
        the thread label and this ``update_label``. A later re-alert replies the
        update on that same thread (so the overspend stays one conversation and
        the W7 offer routes back unbroken), deduped on ``update_label`` so a
        retry never double-posts. It is addressed to ``to`` because the thread's
        latest message is the agent's own, so AgentMail needs the recipients
        spelled out (the same reason the balancer does; SPEC §8).
        """
        existing = self._backend.find_thread(inbox_id, thread_label)
        if existing is None:
            sent = self._backend.send_new(
                inbox_id,
                to,
                subject,
                body,
                labels=[_AGENT_LABEL, thread_label, update_label],
            )
            return sent.thread_id
        if self._backend.find_thread(inbox_id, update_label) is None:
            # ``existing`` came from a labelled message, so the thread is
            # non-empty and ``target`` is its latest message; the None guard
            # only satisfies the type (an empty thread cannot reach here).
            target = self._backend.latest_message_id(inbox_id, existing)
            if target is not None:
                self._backend.send_reply(
                    inbox_id,
                    target,
                    body,
                    labels=[_AGENT_LABEL, update_label],
                    to=to,
                )
        return existing

    def close(self, *, inbox_id: str, thread_id: str) -> None:
        """Mark the transaction's thread closed."""
        self._backend.archive(inbox_id, thread_id)


class _AgentMailBackend:
    """Adapts the AgentMail SDK to the :class:`MailBackend` protocol."""

    def __init__(self, client: AgentMail) -> None:
        self._client = client

    def send_new(
        self,
        inbox_id: str,
        to: list[str],
        subject: str,
        text: str,
        labels: list[str] | None = None,
    ) -> SentEmail:
        result = self._client.inboxes.messages.send(
            inbox_id, to=to, subject=subject, text=text, labels=labels
        )
        return SentEmail(
            message_id=result.message_id, thread_id=result.thread_id
        )

    def send_reply(
        self,
        inbox_id: str,
        message_id: str,
        text: str,
        labels: list[str] | None = None,
        to: list[str] | None = None,
    ) -> SentEmail:
        # ``to`` overrides the recipients; omit it entirely (not None) when
        # unset so AgentMail keeps its own reply-addressing default.
        if to is not None:
            result = self._client.inboxes.messages.reply(
                inbox_id, message_id, text=text, labels=labels, to=to
            )
        else:
            result = self._client.inboxes.messages.reply(
                inbox_id, message_id, text=text, labels=labels
            )
        return SentEmail(
            message_id=result.message_id, thread_id=result.thread_id
        )

    def find_thread(self, inbox_id: str, label: str) -> str | None:
        result = self._client.inboxes.messages.list(
            inbox_id, labels=[label], limit=1
        )
        messages = result.messages
        return messages[0].thread_id if messages else None

    def latest_message_id(self, inbox_id: str, thread_id: str) -> str | None:
        thread = self._client.inboxes.threads.get(inbox_id, thread_id)
        message_id: str | None = thread.last_message_id
        return message_id

    def archive(self, inbox_id: str, thread_id: str) -> None:
        self._client.inboxes.threads.update(
            inbox_id, thread_id, add_labels=["ynab-archived"]
        )
