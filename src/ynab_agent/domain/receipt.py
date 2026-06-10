"""Receipt: a forwarded email or photo, parsed into structured facts.

Receipts and transactions arrive independently, in either order (SPEC §6). A
receipt is parked until the model matches it to a transaction; it carries a
simple status through that join. Its fields come from a data-extraction prompt
and are *facts*, never commands — they can populate a memo or a split, but never
change trust or trigger a budget move (SPEC §0.6).
"""

from __future__ import annotations

import datetime

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.ids import MessageId, ReceiptId, ThreadId
from ynab_agent.domain.money import Money

# How many line items a summary names before collapsing to "+N more", and the
# memo-merge length cap (YNAB memos truncate around 500; stay well under).
_SUMMARY_ITEMS = 3
_MEMO_CAP = 200


class ReceiptLineItem(Frozen):
    """One parsed line of a receipt."""

    description: str
    amount: Money | None = None
    quantity: int | None = None


class Receipt(Frozen):
    """A parked receipt and its join status.

    Attributes:
        id: The receipt's identifier (dedup key for signaling a W2 once).
        merchant: The parsed merchant, if any.
        date: The receipt date, used for date-proximity matching (±1 day).
        total: The receipt total, the strongest single match signal.
        line_items: Parsed items — ground truth for memo and split.
        split_notes: Free-text split hints (e.g. "the $40 is mine").
        status: Where the receipt is in the join lifecycle.
        source_message_id: The inbound email this came from.
        source_thread_id: The email's thread — where the join's questions and
            "no match" note are sent.
        parked_at: When the receipt was parked (for TTL expiry).
    """

    id: ReceiptId
    parked_at: datetime.datetime
    merchant: str | None = None
    date: datetime.date | None = None
    total: Money | None = None
    line_items: tuple[ReceiptLineItem, ...] = ()
    split_notes: str | None = None
    status: ReceiptStatus = ReceiptStatus.PARKED
    source_message_id: MessageId | None = None
    source_thread_id: ThreadId | None = None


def items_brief(receipt: Receipt) -> str | None:
    """The line items as a short human list, or ``None`` when there are none.

    ``"Corn Starch, Paper Towels (+3 more)"`` — the part of a receipt worth
    putting in front of a human or into a memo.
    """
    names = [
        item.description.strip()
        for item in receipt.line_items
        if item.description.strip()
    ]
    if not names:
        return None
    shown = ", ".join(names[:_SUMMARY_ITEMS])
    extra = len(names) - _SUMMARY_ITEMS
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def receipt_summary(receipt: Receipt) -> str:
    """One human line for the receipt: merchant, total, and items.

    Used by the join's emails (ack, disambiguation, no-match) and by W2 when
    it tells the owner a receipt was matched. Pure formatting.
    """
    head = " — ".join(
        part
        for part in (
            receipt.merchant,
            str(receipt.total) if receipt.total is not None else None,
        )
        if part
    )
    items = items_brief(receipt)
    if head and items:
        return f"{head} ({items})"
    return head or items or "a forwarded receipt"


def receipt_memo(receipt: Receipt, current_memo: str | None) -> str:
    """The memo after folding the receipt's detail in. Pure (SPEC §6, §4.4).

    The receipt is ground truth for item detail, but a memo the owner (or a
    prior decision) already wrote must not be clobbered: existing text is kept
    and the items are appended once. Re-folding the same receipt is a no-op
    (the caller compares against the current memo), and the result is capped
    so a long item list never overflows YNAB's memo field.
    """
    detail = items_brief(receipt) or receipt_summary(receipt)
    current = (current_memo or "").strip()
    if not current:
        merged = detail
    elif detail.lower() in current.lower():
        merged = current
    else:
        merged = f"{current} · {detail}"
    return merged[:_MEMO_CAP]
