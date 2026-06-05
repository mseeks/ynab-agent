"""Tests for the W3 inbound classification core (SPEC §5, §0.6)."""

from __future__ import annotations

from ynab_agent.dispatch.classify import (
    Ignore,
    InboundMessage,
    Quarantine,
    RouteToInterpret,
    RouteToOffer,
    RouteToTransaction,
    classify,
)
from ynab_agent.domain.ids import MessageId, ThreadId, YnabTransactionId

_ALLOW = frozenset({"matthew@example.com", "wife@example.com"})


def _msg(
    *,
    sender: str = "matthew@example.com",
    thread: str | None = None,
    verified: bool = True,
    auto: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address=sender,
        subject="[YNAB] $4.50",
        body="ok",
        thread_id=ThreadId(thread) if thread is not None else None,
        signature_verified=verified,
        is_auto_reply=auto,
    )


def test_unsigned_webhook_quarantined() -> None:
    out = classify(_msg(verified=False), _ALLOW, txn_id=None)
    assert isinstance(out, Quarantine)


def test_auto_reply_ignored() -> None:
    out = classify(_msg(auto=True), _ALLOW, txn_id=None)
    assert isinstance(out, Ignore)


def test_mailer_daemon_ignored_even_if_not_allowlisted() -> None:
    out = classify(
        _msg(sender="MAILER-DAEMON@mail.example.com"), _ALLOW, txn_id=None
    )
    assert isinstance(out, Ignore)


def test_unknown_sender_quarantined() -> None:
    out = classify(_msg(sender="stranger@evil.com"), _ALLOW, txn_id=None)
    assert isinstance(out, Quarantine)


def test_reply_on_known_thread_routes_to_transaction() -> None:
    out = classify(_msg(thread="thr1"), _ALLOW, txn_id=YnabTransactionId("t1"))
    assert isinstance(out, RouteToTransaction)
    assert out.txn_id == "t1"


def test_no_thread_routes_to_interpret() -> None:
    out = classify(_msg(), _ALLOW, txn_id=None)
    assert isinstance(out, RouteToInterpret)


def test_offer_thread_routes_to_offer() -> None:
    out = classify(_msg(thread="thr-offer"), _ALLOW, txn_id=None, offer_id="o1")
    assert isinstance(out, RouteToOffer)
    assert out.offer_id == "o1"


def test_transaction_thread_wins_over_offer() -> None:
    out = classify(
        _msg(thread="thr1"),
        _ALLOW,
        txn_id=YnabTransactionId("t1"),
        offer_id="o1",
    )
    assert isinstance(out, RouteToTransaction)


def test_guards_win_before_offer_routing() -> None:
    # An autoresponder / unsigned / non-allowlisted reply on an offer thread is
    # still Ignored/Quarantined first — the inbound boundary precedes routing.
    assert isinstance(
        classify(_msg(auto=True), _ALLOW, txn_id=None, offer_id="o1"), Ignore
    )
    assert isinstance(
        classify(_msg(verified=False), _ALLOW, txn_id=None, offer_id="o1"),
        Quarantine,
    )
    assert isinstance(
        classify(
            _msg(sender="stranger@evil.com"),
            _ALLOW,
            txn_id=None,
            offer_id="o1",
        ),
        Quarantine,
    )


def test_allowlist_is_case_insensitive() -> None:
    out = classify(_msg(sender="Matthew@Example.com"), _ALLOW, txn_id=None)
    assert isinstance(out, RouteToInterpret)


def test_display_name_sender_is_allowlisted() -> None:
    # A mail client may send the From as ``Display Name <addr>`` rather than a
    # bare address; the owner is still allow-listed (regression: such replies
    # were quarantined and the transaction never got categorized).
    out = classify(
        _msg(sender="Matthew Sullivan <matthew@example.com>"),
        _ALLOW,
        txn_id=YnabTransactionId("t1"),
    )
    assert isinstance(out, RouteToTransaction)
    assert out.txn_id == "t1"


def test_display_name_stranger_still_quarantined() -> None:
    out = classify(
        _msg(sender="Sneaky Person <stranger@evil.com>"), _ALLOW, txn_id=None
    )
    assert isinstance(out, Quarantine)
