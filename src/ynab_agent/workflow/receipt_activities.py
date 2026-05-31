"""The I/O ports of the W4 receipt join, as Temporal activities.

Kept in its own module so the join workflow's sandbox import graph stays minimal
(see ``poll_activities`` / ``dispatch_activities``). The match itself is the
agentic step (``match_receipt``); the rest are the spine's side effects. All
stubbed; the real YNAB/AgentMail wiring lands later.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.receipt import Receipt
from ynab_agent.join.match import MatchOutcome

_STUB = "workflow activity stub — register a real or mock implementation"


@activity.defn
async def match_receipt(receipt: Receipt) -> MatchOutcome:
    """Match a receipt against open transactions (the agentic step, SPEC §6)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def signal_match(txn_id: str, receipt_id: str) -> None:
    """Signal-with-start the matched transaction's W2 with this receipt."""
    raise NotImplementedError(_STUB)


@activity.defn
async def ask_disambiguation(receipt_id: str, candidates: list[str]) -> None:
    """Ask the sender which candidate transaction the receipt belongs to."""
    raise NotImplementedError(_STUB)


@activity.defn
async def ask_no_match(receipt_id: str) -> None:
    """Tell the sender no matching transaction was found (TTL expiry)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def save_receipt_status(receipt_id: str, status: ReceiptStatus) -> None:
    """Persist the receipt's new join status so re-checks dedup (SPEC §6)."""
    raise NotImplementedError(_STUB)
