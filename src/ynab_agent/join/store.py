"""The parked-receipt store's pure state and folds (SPEC §6, W4).

The receipt ledger is the join's only persistent memory: which receipts are
parked, and where each is in the join lifecycle (``parked → matched / asked /
expired``). Per the derived-state rule (SPEC §0.5) it lives as Temporal
workflow state — never an external store — and this module is the pure core
the durable :class:`ReceiptLedgerWorkflow` wraps (the same shape
``alert.ledger`` has under ``AlertLedgerWorkflow``): given the current state
and one input, return the next state.

The table is bounded (:data:`LEDGER_CAP`): once full, the oldest *terminal*
receipts (matched/expired) age off first — they are kept at all only so a
re-check or a webhook replay stays a no-op — and only then the oldest open
ones, so continued forwarding can never grow the carried state without limit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.receipt import Receipt

if TYPE_CHECKING:
    from collections.abc import Iterable

# Generous for a household — a receipt is one forwarded email.
LEDGER_CAP = 100

# The statuses a W1 re-check still acts on: PARKED waits for its transaction;
# ASKED still accepts a late confident match (plan_join allows it — the human
# or a new posting may have resolved the ambiguity).
_OPEN_STATUSES = frozenset({ReceiptStatus.PARKED, ReceiptStatus.ASKED})


class ReceiptLedgerState(Frozen):
    """The parked-receipt table carried across continue-as-new."""

    receipts: tuple[Receipt, ...] = ()


def _bounded(receipts: Iterable[Receipt]) -> tuple[Receipt, ...]:
    """The table capped to :data:`LEDGER_CAP`, terminal receipts aging first."""
    table = tuple(receipts)
    if len(table) <= LEDGER_CAP:
        return table
    overflow = len(table) - LEDGER_CAP
    terminal_oldest_first = sorted(
        (r for r in table if r.status not in _OPEN_STATUSES),
        key=lambda r: r.parked_at,
    )
    drop = {r.id for r in terminal_oldest_first[:overflow]}
    kept = tuple(r for r in table if r.id not in drop)
    # Pathological: more open receipts than the cap — drop the oldest anyway.
    return kept[-LEDGER_CAP:]


def park(state: ReceiptLedgerState, receipt: Receipt) -> ReceiptLedgerState:
    """Add a receipt to the table; an already-known id is left untouched.

    Idempotent on the receipt id (derived from the inbound message id), so a
    webhook retry or a re-forward of the same email never resets a receipt's
    join status. Pure.
    """
    if any(r.id == receipt.id for r in state.receipts):
        return state
    return ReceiptLedgerState(receipts=_bounded((*state.receipts, receipt)))


def set_status(
    state: ReceiptLedgerState, receipt_id: str, status: ReceiptStatus
) -> ReceiptLedgerState:
    """Move one receipt to ``status``; unknown ids are a no-op. Pure."""
    updated = tuple(
        r.model_copy(update={"status": status})
        if str(r.id) == receipt_id
        else r
        for r in state.receipts
    )
    if updated == state.receipts:
        return state
    return ReceiptLedgerState(receipts=updated)


def get(state: ReceiptLedgerState, receipt_id: str) -> Receipt | None:
    """The receipt with ``receipt_id``, or ``None``. Pure."""
    return next((r for r in state.receipts if str(r.id) == receipt_id), None)


def open_receipts(state: ReceiptLedgerState) -> tuple[Receipt, ...]:
    """The receipts a W1 re-check should still attempt (SPEC §6). Pure."""
    return tuple(r for r in state.receipts if r.status in _OPEN_STATUSES)
