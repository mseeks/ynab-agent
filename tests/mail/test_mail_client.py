"""Tests for the AgentMail client (SPEC §5).

Offline tests inject a fake backend (no network). One opt-in live test sends a
real email through AgentMail and is skipped unless ``YNAB_AGENT_LIVE_EMAIL`` is
set, so the gate stays offline.

The fake backend models the one property the client relies on for store-free
idempotency: labels are sticky per thread, so ``find_thread(label)`` re-finds an
already-opened thread (open dedup) or an already-sent action (send dedup).
"""

from __future__ import annotations

import os

import pytest

from ynab_agent.mail.client import MailClient, OutboundEmail, SentEmail


class _FakeBackend:
    """An in-memory AgentMail: threads, sticky labels, last-message tracking."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._labels: dict[str, str] = {}  # label -> thread_id (first to carry)
        self._last: dict[str, str] = {}  # thread_id -> last message_id
        self._msg_thread: dict[str, str] = {}  # message_id -> thread_id
        self._archived: set[str] = set()
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _register(self, thread_id: str, labels: list[str] | None) -> None:
        for label in labels or []:
            self._labels.setdefault(label, thread_id)

    def send_new(
        self,
        inbox_id: str,
        to: list[str],
        subject: str,
        text: str,
        labels: list[str] | None = None,
    ) -> SentEmail:
        self.calls.append(("new", inbox_id, ",".join(to), subject, text))
        thread_id = self._next("t")
        message_id = self._next("m")
        self._last[thread_id] = message_id
        self._msg_thread[message_id] = thread_id
        self._register(thread_id, labels)
        return SentEmail(message_id=message_id, thread_id=thread_id)

    def send_reply(
        self,
        inbox_id: str,
        message_id: str,
        text: str,
        labels: list[str] | None = None,
    ) -> SentEmail:
        self.calls.append(("reply", inbox_id, message_id, text))
        thread_id = self._msg_thread.get(message_id, "t1")
        new_id = self._next("m")
        self._last[thread_id] = new_id
        self._msg_thread[new_id] = thread_id
        self._register(thread_id, labels)
        return SentEmail(message_id=new_id, thread_id=thread_id)

    def find_thread(self, inbox_id: str, label: str) -> str | None:
        return self._labels.get(label)

    def latest_message_id(self, inbox_id: str, thread_id: str) -> str | None:
        return self._last.get(thread_id)

    def archive(self, inbox_id: str, thread_id: str) -> None:
        self.calls.append(("archive", inbox_id, thread_id))
        self._archived.add(thread_id)


def test_new_thread_routes_to_send_new() -> None:
    backend = _FakeBackend()
    out = MailClient(backend).send(
        OutboundEmail(
            inbox_id="ib",
            to=("wife@example.com",),
            subject="[YNAB] $4.50",
            text="best guess: Dining",
        )
    )
    assert out.thread_id
    assert backend.calls[0][0] == "new"
    assert backend.calls[0][3] == "[YNAB] $4.50"


def test_reply_routes_to_send_reply() -> None:
    backend = _FakeBackend()
    out = MailClient(backend).send(
        OutboundEmail(
            inbox_id="ib",
            subject="[YNAB] $4.50",
            text="applied, thanks",
            reply_to_message_id="m0",
        )
    )
    assert out.message_id
    assert backend.calls[0] == ("reply", "ib", "m0", "applied, thanks")


def test_new_thread_without_recipients_is_rejected() -> None:
    with pytest.raises(ValueError, match="recipient"):
        OutboundEmail(inbox_id="ib", subject="x", text="y")


def test_open_thread_sends_once_then_dedups() -> None:
    backend = _FakeBackend()
    client = MailClient(backend)
    first = client.open_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="[YNAB] $4.50",
        body="best guess: Dining",
        txn_label="yatxn-abc",
    )
    second = client.open_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="[YNAB] $4.50",
        body="best guess: Dining",
        txn_label="yatxn-abc",
    )
    assert first == second
    # Only the first call actually sent a new thread (idempotent open).
    assert [c[0] for c in backend.calls] == ["new"]


def test_send_on_thread_dedups_on_seq_label() -> None:
    backend = _FakeBackend()
    client = MailClient(backend)
    thread_id = client.open_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="[YNAB] $4.50",
        body="proposal",
        txn_label="yatxn-abc",
    )
    sent_first = client.send_on_thread(
        inbox_id="ib",
        thread_id=thread_id,
        body="a reminder",
        seq_label="yaseq-abc-2",
    )
    sent_again = client.send_on_thread(
        inbox_id="ib",
        thread_id=thread_id,
        body="a reminder",
        seq_label="yaseq-abc-2",
    )
    assert sent_first is True
    assert sent_again is False
    assert [c[0] for c in backend.calls].count("reply") == 1


def test_send_on_thread_false_when_thread_empty() -> None:
    backend = _FakeBackend()
    sent = MailClient(backend).send_on_thread(
        inbox_id="ib",
        thread_id="t-unknown",
        body="hi",
        seq_label="yaseq-x-1",
    )
    assert sent is False
    assert backend.calls == []


def test_close_archives_the_thread() -> None:
    backend = _FakeBackend()
    MailClient(backend).close(inbox_id="ib", thread_id="t1")
    assert backend.calls == [("archive", "ib", "t1")]


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_EMAIL"),
    reason="set YNAB_AGENT_LIVE_EMAIL=1 to send a real AgentMail message",
)
def test_live_send_reaches_the_owner() -> None:
    inbox = os.environ.get("YNAB_AGENT_INBOX", "alivehoney93@agentmail.to")
    to = os.environ.get("YNAB_AGENT_OWNER", "matthew@mseeks.me")
    out = MailClient.from_env().send(
        OutboundEmail(
            inbox_id=inbox,
            to=(to,),
            subject="[YNAB agent] live mail smoke",
            text="This is the YNAB agent's AgentMail send path, working.",
        )
    )
    assert out.message_id
    assert out.thread_id
