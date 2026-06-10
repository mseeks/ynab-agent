"""Effects: data describing what the spine should do after a transition.

The state machine is pure and performs no I/O. It returns *effects* — commit a
write, send a thread message, set or cancel a timer, feed rule learning. The
Temporal spine interprets and executes them (commit→verify, idempotent sends,
durable timers). Effects are values, so a transition is fully testable without
touching YNAB or email.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.signals import InboundSignal


class MessagePurpose(StrEnum):
    """Why a thread message is being sent (shapes the prose; SPEC §3, §5)."""

    PROPOSAL = "proposal"
    FYI = "fyi"
    CONFIRM = "confirm"
    CLARIFY = "clarify"
    HANDOFF = "handoff"
    REVISE_SUMMARY = "revise_summary"
    POSSIBLY_INCONSISTENT = "possibly_inconsistent"
    DIVERGED_READBACK = "diverged_readback"
    ARCHIVE_NOTICE = "archive_notice"
    OVERRIDE_NOTICE = "override_notice"


class TimerKind(StrEnum):
    """The lifecycle timers (SPEC §3)."""

    AMAZON_HOLD = "amazon_hold"
    PATIENCE = "patience"
    ARCHIVE = "archive"


class RuleLearningKind(StrEnum):
    """The human-decision events rule learning consumes (SPEC §9)."""

    CONFIRM = "confirm"
    CORRECT = "correct"


class OpenThread(Frozen):
    """Create the AgentMail thread for this transaction (SPEC §5)."""

    kind: Literal["open_thread"] = "open_thread"


class SendThreadMessage(Frozen):
    """Send a message on the transaction's thread.

    ``detail`` is the message-specific content the template wraps (the model's
    clarifying question, a diverged which-wins comparison); ``decision`` lets
    the send name what was actually written (the confirm/FYI/revise-summary
    category). Without these the spine once rendered every post-proposal email
    as a contentless placeholder — the payloads were computed, then dropped.
    """

    kind: Literal["send_message"] = "send_message"
    purpose: MessagePurpose
    detail: str | None = None
    decision: Decision | None = None


class CommitToYnab(Frozen):
    """Commit a decision to YNAB (the spine does commit→verify; SPEC §0.5)."""

    kind: Literal["commit_to_ynab"] = "commit_to_ynab"
    decision: Decision


class SetTimer(Frozen):
    """Arm a durable timer with an absolute deadline."""

    kind: Literal["set_timer"] = "set_timer"
    timer: TimerKind
    deadline: datetime.datetime


class CancelTimer(Frozen):
    """Cancel a previously armed timer."""

    kind: Literal["cancel_timer"] = "cancel_timer"
    timer: TimerKind


class FeedRuleLearning(Frozen):
    """Feed a confirm/correct event to rule learning (W5; SPEC §9).

    ``payee`` is carried so W5 can key the rule's match on it without re-reading
    the snapshot. For a correction, ``prior`` carries the decision being
    overturned, so W5 can demote *the rule that produced the prior decision*
    (§3 rule 6), not the new one.
    """

    kind: Literal["feed_rule_learning"] = "feed_rule_learning"
    event: RuleLearningKind
    payee: str
    decision: Decision | None = None
    prior: Decision | None = None


class RecordAutoAction(Frozen):
    """Record a landed auto-action in the circuit-breaker ledger (SPEC §0.6).

    Emitted alongside the commit when a blessed rule auto-applies, so the hard
    floor's per-run / per-day counts are real and the breaker can trip. Keyed by
    ``ynab_id`` so a retry or re-enrichment counts the transaction once.
    """

    kind: Literal["record_auto_action"] = "record_auto_action"
    ynab_id: str


class ReplayBuffered(Frozen):
    """Re-deliver the signals buffered while in DISCOVERED (SPEC §3)."""

    kind: Literal["replay_buffered"] = "replay_buffered"
    signals: tuple[InboundSignal, ...]


class CloseThread(Frozen):
    """Label and close the AgentMail thread on archive (SPEC §13)."""

    kind: Literal["close_thread"] = "close_thread"


Effect = Annotated[
    OpenThread
    | SendThreadMessage
    | CommitToYnab
    | SetTimer
    | CancelTimer
    | FeedRuleLearning
    | RecordAutoAction
    | ReplayBuffered
    | CloseThread,
    Field(discriminator="kind"),
]
