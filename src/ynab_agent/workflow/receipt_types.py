"""Value types for the W4 receipt-join workflow."""

from __future__ import annotations

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.receipt import Receipt


class ReceiptJoinParams(Frozen):
    """The W4 params: the parked receipt to (re-)attempt a join for."""

    receipt: Receipt


class ReceiptJoinResult(Frozen):
    """What the join did, and the receipt's resulting store status."""

    action: str
    status: ReceiptStatus
