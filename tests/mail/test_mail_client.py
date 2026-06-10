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
        self.reply_tos: list[list[str] | None] = []  # `to` per send_reply call
        self.htmls: list[str | None] = []  # `html` per send call, in order
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
        html: str | None = None,
    ) -> SentEmail:
        self.calls.append(("new", inbox_id, ",".join(to), subject, text))
        self.htmls.append(html)
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
        to: list[str] | None = None,
        html: str | None = None,
    ) -> SentEmail:
        self.calls.append(("reply", inbox_id, message_id, text))
        self.reply_tos.append(to)
        self.htmls.append(html)
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


def test_send_on_thread_forwards_recipients_to_the_reply() -> None:
    # The balancer replies on the W6 alert thread but must address the owners
    # explicitly, else AgentMail loops the reply back to the agent (SPEC §8).
    backend = _FakeBackend()
    client = MailClient(backend)
    thread_id = client.open_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: over budget",
        body="alert",
        txn_label="yaspend-dining",
    )
    sent = client.send_on_thread(
        inbox_id="ib",
        thread_id=thread_id,
        body="cover the overspend?",
        seq_label="yb-cover-dining",
        to=["wife@example.com"],
    )
    assert sent is True
    assert backend.reply_tos == [["wife@example.com"]]


def test_send_on_thread_without_recipients_omits_the_override() -> None:
    # The other callers (W2, the autonomy offer) reply only after the owner has
    # spoken, so they leave AgentMail's default reply-addressing in place.
    backend = _FakeBackend()
    client = MailClient(backend)
    thread_id = client.open_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="[YNAB] $4.50",
        body="proposal",
        txn_label="yatxn-abc",
    )
    client.send_on_thread(
        inbox_id="ib",
        thread_id=thread_id,
        body="a reminder",
        seq_label="yaseq-abc-2",
    )
    assert backend.reply_tos == [None]


def test_alert_on_thread_opens_then_replies_on_the_same_thread() -> None:
    # First alert opens the thread; a worsening re-alert (new update label, same
    # thread label) replies on that SAME thread, not a new one. The regression
    # guard for re-alert orphaning: one conversation, so a reply always routes
    # back to the W7 balancer indexed by that thread (SPEC §7).
    backend = _FakeBackend()
    client = MailClient(backend)
    first = client.alert_on_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: trending over budget",
        body="Dining is trending over budget...",
        thread_label="yaspend-dining-2026-06",
        update_label="yaspend-update-dining-2026-06-trending_over-500000",
    )
    second = client.alert_on_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: already over budget",
        body="Dining is already over budget...",
        thread_label="yaspend-dining-2026-06",
        update_label="yaspend-update-dining-2026-06-already_over-560000",
    )
    assert first == second  # one conversation across the re-alert
    assert [c[0] for c in backend.calls] == ["new", "reply"]
    # The re-alert reply is addressed to the owner — the thread's latest message
    # is the agent's own opening alert, so without this it would loop back.
    assert backend.reply_tos == [["wife@example.com"]]


def test_alert_on_thread_dedups_a_retried_alert() -> None:
    # A retry of the first alert (same thread + update label) re-sends nothing:
    # the update label rides on the opening message, so the retry's reply branch
    # finds it already on record and skips.
    backend = _FakeBackend()
    client = MailClient(backend)
    client.alert_on_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: trending over budget",
        body="alert",
        thread_label="yaspend-dining-2026-06",
        update_label="yaspend-update-dining-2026-06-trending_over-500000",
    )
    client.alert_on_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: trending over budget",
        body="alert",
        thread_label="yaspend-dining-2026-06",
        update_label="yaspend-update-dining-2026-06-trending_over-500000",
    )
    assert [c[0] for c in backend.calls] == ["new"]  # no duplicate send


def test_alert_on_thread_dedups_a_retried_re_alert() -> None:
    # First alert opens; a worsening re-alert replies; a retry of THAT re-alert
    # (same update label) re-sends nothing. Proves the update label is recorded
    # on the reply message too, so the dedup holds for replies, not just opens.
    backend = _FakeBackend()
    client = MailClient(backend)
    client.alert_on_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: trending over budget",
        body="first alert",
        thread_label="yaspend-dining-2026-06",
        update_label="yaspend-update-dining-2026-06-trending_over-500000",
    )
    client.alert_on_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: already over budget",
        body="worse now",
        thread_label="yaspend-dining-2026-06",
        update_label="yaspend-update-dining-2026-06-already_over-560000",
    )
    client.alert_on_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="Dining: already over budget",
        body="worse now",
        thread_label="yaspend-dining-2026-06",
        update_label="yaspend-update-dining-2026-06-already_over-560000",
    )
    assert [c[0] for c in backend.calls] == ["new", "reply"]  # retry no-op


def test_every_send_carries_an_html_part() -> None:
    # No email lands as raw unstyled text: when the caller passes no html, a
    # clean typographic rendering is derived from the text part.
    backend = _FakeBackend()
    client = MailClient(backend)
    thread_id = client.open_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="s",
        body="best guess: Dining",
        txn_label="yatxn-abc",
    )
    client.send_on_thread(
        inbox_id="ib",
        thread_id=thread_id,
        body="a follow-up",
        seq_label="yaseq-abc-2",
    )
    assert len(backend.htmls) == 2
    assert backend.htmls[0] is not None
    assert "best guess: Dining" in backend.htmls[0]
    assert backend.htmls[1] is not None
    assert "a follow-up" in backend.htmls[1]


def test_explicit_html_overrides_the_derived_default() -> None:
    backend = _FakeBackend()
    MailClient(backend).open_thread(
        inbox_id="ib",
        to=["wife@example.com"],
        subject="s",
        body="plain words",
        txn_label="yatxn-x",
        html="<div>styled card</div>",
    )
    assert backend.htmls == ["<div>styled card</div>"]


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


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_EMAIL"),
    reason="set YNAB_AGENT_LIVE_EMAIL=1 to send real AgentMail messages",
)
def test_live_reply_threads_and_addresses_the_owner() -> None:
    """The W7 fix's load-bearing claim, against the real server (SPEC §8).

    Reproduce the W6→W7 shape: open an alert thread (the agent's own outbound),
    then reply on it addressed to the owner — the exact case commit a60e676
    found broke (the reply's ``To`` looped back to the agent, so the owner never
    saw it). Assert the reply lands on the *same* thread (AgentMail threaded it)
    and its ``To`` is the owner (delivered, not looped back). These two server
    behaviors are what the offline fake cannot prove.
    """
    from agentmail import AgentMail

    inbox = os.environ.get("YNAB_AGENT_INBOX", "alivehoney93@agentmail.to")
    owner = os.environ.get("YNAB_AGENT_OWNER", "matthew@mseeks.me")
    nonce = os.urandom(4).hex()

    mail = MailClient.from_env()
    thread = mail.open_thread(
        inbox_id=inbox,
        to=[owner],
        subject="[YNAB agent] live reply-threading smoke",
        body="alert: Dining is over budget.",
        txn_label=f"live-alert-{nonce}",
    )
    sent = mail.send_on_thread(
        inbox_id=inbox,
        thread_id=thread,
        body="Cover the overspend? (live reply-threading test.)",
        seq_label=f"live-cover-{nonce}",
        to=[owner],
    )
    assert sent is True

    raw = AgentMail(api_key=os.environ["AGENTMAIL_API_KEY"])
    last_id = raw.inboxes.threads.get(inbox, thread).last_message_id
    reply = raw.inboxes.messages.get(inbox, last_id)
    # Threaded on the server, and addressed to the owner (not looped back to the
    # agent — the a60e676 symptom). ``in_reply_to`` confirms the threading
    # headers the owner's mail client needs are set.
    assert reply.thread_id == thread
    assert owner in str(reply.to)
    assert reply.in_reply_to is not None
