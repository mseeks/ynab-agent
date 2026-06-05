"""AgentMail reader: the recent conversation surface (best-effort).

Lists the inbox's recent threads via the AgentMail SDK, run off the event loop
in a worker thread, and reduces each to a one-line summary tagged by the agent's
own labels (a ``yaoffer-`` thread is an autonomy offer, a ``yatxn-`` thread a
transaction proposal, else a plain thread). Read-only; degrades to "off" when
``AGENTMAIL_API_KEY`` is unset and to a red dot on any API hiccup.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ynab_agent.dashboard.model import Conversation

_MAX_THREADS = 12


def _kind(labels: tuple[str, ...]) -> str:
    """Classify a thread from the agent's own idempotency labels."""
    if any(label.startswith("yaoffer-") for label in labels):
        return "offer"
    if any(label.startswith("yatxn-") for label in labels):
        return "proposal"
    return "thread"


def _timestamp(item: Any) -> object:
    """The most recent timestamp on a thread item (SDK-shape tolerant)."""
    for attr in (
        "updated_at",
        "timestamp",
        "sent_timestamp",
        "received_timestamp",
    ):
        value = getattr(item, attr, None)
        if value is not None:
            return value
    return None


def _read(inbox: str, key: str) -> tuple[Conversation, ...]:
    """List recent threads synchronously (run in a worker thread)."""
    from agentmail import AgentMail

    client = AgentMail(api_key=key)
    result = client.inboxes.threads.list(inbox, limit=_MAX_THREADS)
    threads = getattr(result, "threads", None) or []
    conversations: list[Conversation] = []
    for item in threads:
        labels = tuple(str(x) for x in (getattr(item, "labels", None) or ()))
        conversations.append(
            Conversation(
                subject=str(getattr(item, "subject", "") or "(no subject)"),
                preview=str(getattr(item, "preview", "") or "")[:160],
                kind=_kind(labels),
                updated_at=_timestamp(item),  # type: ignore[arg-type]
            )
        )
    return tuple(conversations)


async def fetch() -> tuple[tuple[Conversation, ...], str | None]:
    """List recent conversations; ``error`` is "off" when unconfigured."""
    key = os.environ.get("AGENTMAIL_API_KEY")
    inbox = os.environ.get("YNAB_AGENT_INBOX")
    if not key or not inbox:
        return (), "off"
    try:
        conversations = await asyncio.to_thread(_read, inbox, key)
    except Exception as exc:  # any AgentMail hiccup degrades to a red dot
        return (), f"{type(exc).__name__}: {exc}"
    return conversations, None
