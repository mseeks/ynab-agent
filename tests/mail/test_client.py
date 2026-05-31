"""Tests for the AgentMail client (SPEC §5).

Offline tests inject a fake backend (no network). One opt-in live test sends a
real email through AgentMail and is skipped unless ``YNAB_AGENT_LIVE_EMAIL`` is
set, so the gate stays offline.
"""

from __future__ import annotations

import os

import pytest

from ynab_agent.mail.client import MailClient, OutboundEmail, SentEmail


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def send_new(
        self, inbox_id: str, to: list[str], subject: str, text: str
    ) -> SentEmail:
        self.calls.append(("new", inbox_id, ",".join(to), subject, text))
        return SentEmail(message_id="m1", thread_id="t1")

    def send_reply(
        self, inbox_id: str, message_id: str, text: str
    ) -> SentEmail:
        self.calls.append(("reply", inbox_id, message_id, text))
        return SentEmail(message_id="m2", thread_id="t1")


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
    assert out.thread_id == "t1"
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
    assert out.message_id == "m2"
    assert backend.calls[0] == ("reply", "ib", "m0", "applied, thanks")


def test_new_thread_without_recipients_is_rejected() -> None:
    with pytest.raises(ValueError, match="recipient"):
        OutboundEmail(inbox_id="ib", subject="x", text="y")


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
