"""Value types exchanged across the workflow boundary.

The workflow start params (including the resumable state carried across
``continue-as-new``) and the interpreted outcome of a human reply — the agentic
middle's verdict that the deterministic spine then acts on.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.effects import TimerKind
from ynab_agent.domain.ids import ThreadId, YnabTransactionId
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.signals import InboundSignal
from ynab_agent.domain.transaction import Transaction


class AnswerOutcome(Frozen):
    """A reply interpreted as a decision to commit."""

    kind: Literal["answer"] = "answer"
    decision: Decision


class ClarifyOutcome(Frozen):
    """A reply that needs a follow-up question before deciding."""

    kind: Literal["clarify"] = "clarify"
    question: str


ReplyOutcome = Annotated[
    AnswerOutcome | ClarifyOutcome, Field(discriminator="kind")
]


class TransactionParams(Frozen):
    """The W2 workflow's start params.

    A fresh transaction sets only ``ynab_id`` (and maybe ``thread_id``). The
    ``resume_*`` fields carry the durable state across ``continue-as-new`` so a
    long-lived transaction survives without unbounded history.
    """

    ynab_id: YnabTransactionId
    thread_id: ThreadId | None = None
    resume_txn: Transaction | None = None
    resume_deadlines: dict[TimerKind, datetime.datetime] = Field(
        default_factory=dict
    )
    resume_inbound: tuple[InboundSignal, ...] = ()
