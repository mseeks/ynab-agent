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
from ynab_agent.domain.ids import MessageId, ReceiptId
from ynab_agent.domain.money import Money


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
