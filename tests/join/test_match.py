"""Tests for the W4 receipt-join spine (SPEC §6).

The matching itself is the model's job; these pin the spine's guarantees:
act once, ask once, resolve ambiguity to a question, and age out at the TTL.
"""

from __future__ import annotations

import datetime

from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.ids import ReceiptId, YnabTransactionId
from ynab_agent.domain.receipt import Receipt
from ynab_agent.join.match import (
    Ambiguous,
    AskDisambiguation,
    AskNoMatch,
    ConfidentMatch,
    DoNothing,
    NoMatch,
    Park,
    SignalTransaction,
    plan_join,
    resulting_status,
)

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)
_T1 = YnabTransactionId("t1")
_T2 = YnabTransactionId("t2")


def _receipt(
    *,
    status: ReceiptStatus = ReceiptStatus.PARKED,
    parked: datetime.datetime = _NOW,
) -> Receipt:
    return Receipt(id=ReceiptId("r1"), parked_at=parked, status=status)


def test_confident_match_signals_the_transaction() -> None:
    action = plan_join(_receipt(), ConfidentMatch(txn_id=_T1), now=_NOW)
    assert isinstance(action, SignalTransaction)
    assert action.txn_id == _T1
    assert action.receipt_id == "r1"
    assert resulting_status(action) is ReceiptStatus.MATCHED


def test_ambiguous_asks_once() -> None:
    action = plan_join(_receipt(), Ambiguous(candidates=(_T1, _T2)), now=_NOW)
    assert isinstance(action, AskDisambiguation)
    assert action.candidates == (_T1, _T2)
    assert resulting_status(action) is ReceiptStatus.ASKED


def test_ambiguous_does_not_re_ask_after_asked() -> None:
    action = plan_join(
        _receipt(status=ReceiptStatus.ASKED),
        Ambiguous(candidates=(_T1, _T2)),
        now=_NOW,
    )
    assert isinstance(action, DoNothing)
    assert resulting_status(action) is None


def test_confident_match_overrides_an_earlier_ambiguous_ask() -> None:
    # A human reply or a fresh posting resolved the ambiguity → still signal.
    action = plan_join(
        _receipt(status=ReceiptStatus.ASKED),
        ConfidentMatch(txn_id=_T1),
        now=_NOW,
    )
    assert isinstance(action, SignalTransaction)


def test_no_match_within_ttl_parks() -> None:
    action = plan_join(_receipt(), NoMatch(), now=_NOW)
    assert isinstance(action, Park)
    assert resulting_status(action) is None


def test_no_match_past_ttl_ages_out() -> None:
    parked = _NOW - datetime.timedelta(days=31)
    action = plan_join(_receipt(parked=parked), NoMatch(), now=_NOW)
    assert isinstance(action, AskNoMatch)
    assert resulting_status(action) is ReceiptStatus.EXPIRED


def test_matched_receipt_is_terminal() -> None:
    action = plan_join(
        _receipt(status=ReceiptStatus.MATCHED),
        ConfidentMatch(txn_id=_T2),
        now=_NOW,
    )
    assert isinstance(action, DoNothing)


def test_expired_receipt_is_terminal() -> None:
    parked = _NOW - datetime.timedelta(days=99)
    action = plan_join(
        _receipt(status=ReceiptStatus.EXPIRED, parked=parked),
        NoMatch(),
        now=_NOW,
    )
    assert isinstance(action, DoNothing)


def test_asked_receipt_ages_out_at_the_ttl() -> None:
    # Asked once and still ambiguous: closure arrives at the TTL instead of
    # silent hourly re-matching forever.
    import datetime as dt

    from ynab_agent.domain.enums import ReceiptStatus
    from ynab_agent.join.match import AskNoMatch, DoNothing

    asked = _receipt(status=ReceiptStatus.ASKED)
    fresh_now = asked.parked_at + dt.timedelta(days=1)
    stale_now = asked.parked_at + dt.timedelta(days=31)
    still_ambiguous = Ambiguous(
        candidates=(YnabTransactionId("t1"), YnabTransactionId("t2"))
    )
    assert isinstance(
        plan_join(asked, still_ambiguous, now=fresh_now), DoNothing
    )
    aged = plan_join(asked, still_ambiguous, now=stale_now)
    assert isinstance(aged, AskNoMatch)
