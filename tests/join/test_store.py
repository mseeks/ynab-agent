"""Tests for the parked-receipt store's pure folds (SPEC §6, W4)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.ids import ReceiptId
from ynab_agent.domain.receipt import Receipt
from ynab_agent.join.store import (
    LEDGER_CAP,
    ReceiptLedgerState,
    get,
    open_receipts,
    park,
    set_status,
)

_NOW = datetime.datetime(2026, 6, 10, 12, 0, tzinfo=datetime.UTC)


def _receipt(
    rid: str = "r1",
    *,
    status: ReceiptStatus = ReceiptStatus.PARKED,
    at: datetime.datetime = _NOW,
) -> Receipt:
    return Receipt(id=ReceiptId(rid), parked_at=at, status=status)


def test_park_adds_and_is_idempotent_on_the_id() -> None:
    state = park(ReceiptLedgerState(), _receipt())
    assert [str(r.id) for r in state.receipts] == ["r1"]
    # A webhook retry re-parks the same id after the join already matched it:
    # the existing entry (and its status) must survive untouched.
    matched = set_status(state, "r1", ReceiptStatus.MATCHED)
    again = park(matched, _receipt())
    assert again is matched
    assert again.receipts[0].status is ReceiptStatus.MATCHED


def test_set_status_moves_one_and_ignores_unknown_ids() -> None:
    state = park(ReceiptLedgerState(), _receipt())
    asked = set_status(state, "r1", ReceiptStatus.ASKED)
    assert asked.receipts[0].status is ReceiptStatus.ASKED
    assert set_status(asked, "nope", ReceiptStatus.MATCHED) is asked


def test_get_and_open_receipts() -> None:
    state = ReceiptLedgerState()
    state = park(state, _receipt("parked"))
    state = park(state, _receipt("asked", status=ReceiptStatus.ASKED))
    state = park(state, _receipt("done", status=ReceiptStatus.MATCHED))
    state = park(state, _receipt("aged", status=ReceiptStatus.EXPIRED))
    assert get(state, "done") is not None
    assert get(state, "missing") is None
    # Re-checks act on parked AND asked (a late confident match still lands);
    # matched/expired are terminal.
    assert {str(r.id) for r in open_receipts(state)} == {"parked", "asked"}


def test_cap_ages_terminal_receipts_out_first() -> None:
    state = ReceiptLedgerState()
    old = _NOW - datetime.timedelta(days=20)
    state = park(
        state, _receipt("oldest-done", status=ReceiptStatus.MATCHED, at=old)
    )
    for i in range(LEDGER_CAP):
        state = park(state, _receipt(f"open-{i}"))
    assert len(state.receipts) == LEDGER_CAP
    assert get(state, "oldest-done") is None  # terminal aged out first
    assert get(state, "open-0") is not None  # open receipts all survive
