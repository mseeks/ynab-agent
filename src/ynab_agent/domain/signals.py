"""Raw inbound signals — the external input W3/W4 deliver to a transaction.

These are *uninterpreted*: a reply has arrived, or a receipt matched. The model
interprets them (into a decision, a clarifying question, or a revision target);
the state machine only routes the raw arrival by current state — buffering it
before a snapshot exists, reopening a resting transaction, and so on (SPEC §3).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import MessageId, ReceiptId, ThreadId


class ReplySignal(Frozen):
    """A human reply landing on a transaction's thread."""

    kind: Literal["reply"] = "reply"
    thread_id: ThreadId
    message_id: MessageId
    from_address: str
    text: str


class ReceiptSignal(Frozen):
    """A receipt the join (W4) matched to this transaction."""

    kind: Literal["receipt"] = "receipt"
    receipt_id: ReceiptId


InboundSignal = Annotated[
    ReplySignal | ReceiptSignal, Field(discriminator="kind")
]
