"""Lifecycle events: the decided inputs that drive the state machine (SPEC §3).

Most events carry an *already-decided* policy result: the model interprets a
reply, the autonomy gate chooses auto-vs-ask, the converge step computes its
outcome — and only then does an event reach the state machine, which owns
*when* and *where*, never *what*. Raw, uninterpreted input arrives as
:class:`~.signals.InboundSignal` wrapped in :class:`InboundReceived`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.proposal import Decision, Proposal
from ynab_agent.domain.signals import InboundSignal
from ynab_agent.domain.transaction import YnabSnapshot


class VerifyOutcome(StrEnum):
    """The read-after-write verdict for a committed write (SPEC §3)."""

    MATCH = "match"
    COULD_NOT_CONFIRM = "could_not_confirm"
    DIVERGED = "diverged"


# ── Enrichment outcome (ENRICHING resolution) ───────────────────────────────
class AutoApply(Frozen):
    """The gate granted auto-apply: a concrete decision is ready to commit."""

    kind: Literal["auto_apply"] = "auto_apply"
    decision: Decision


class AskHuman(Frozen):
    """The gate requires a human: email the proposal and wait."""

    kind: Literal["ask_human"] = "ask_human"
    proposal: Proposal


EnrichmentOutcome = Annotated[AutoApply | AskHuman, Field(discriminator="kind")]


# ── Converge outcome (REVISING resolution; SPEC §3) ─────────────────────────
class Reapplied(Frozen):
    """The target was written and verified; the transaction is re-decided."""

    kind: Literal["reapplied"] = "reapplied"
    decision: Decision


class NoChange(Frozen):
    """Current YNAB state already equals the target; nothing was written."""

    kind: Literal["no_change"] = "no_change"


class CouldNotConfirm(Frozen):
    """Retries exhausted; the write may or may not have landed."""

    kind: Literal["could_not_confirm"] = "could_not_confirm"


class Diverged(Frozen):
    """YNAB shows a different non-empty state than the instruction asked for."""

    kind: Literal["diverged"] = "diverged"
    ynab_summary: str
    requested_summary: str


class NeedsHuman(Frozen):
    """The reconciliation guard or an ambiguity routes this to a human."""

    kind: Literal["needs_human"] = "needs_human"
    reason: str


ConvergeOutcome = Annotated[
    Reapplied | NoChange | CouldNotConfirm | Diverged | NeedsHuman,
    Field(discriminator="kind"),
]


# ── The lifecycle events ────────────────────────────────────────────────────
class SnapshotMaterialized(Frozen):
    """W1 polled the YNAB snapshot; the transaction can leave DISCOVERED."""

    kind: Literal["snapshot_materialized"] = "snapshot_materialized"
    snapshot: YnabSnapshot
    hold_for_amazon: bool = False


class SnapshotUnavailable(Frozen):
    """The signal beat the poll; stay in DISCOVERED and keep buffering."""

    kind: Literal["snapshot_unavailable"] = "snapshot_unavailable"


class InboundReceived(Frozen):
    """A raw inbound signal arrived (reply or matched receipt)."""

    kind: Literal["inbound_received"] = "inbound_received"
    signal: InboundSignal


class HoldResolved(Frozen):
    """The Amazon memo backfilled or a receipt matched; resume enrichment."""

    kind: Literal["hold_resolved"] = "hold_resolved"
    snapshot: YnabSnapshot


class HoldDeadlineReached(Frozen):
    """The Amazon hold expired; enrich and fall back to asking (SPEC §3)."""

    kind: Literal["hold_deadline"] = "hold_deadline"


class Enriched(Frozen):
    """Enrichment finished; the gate chose auto-apply or ask."""

    kind: Literal["enriched"] = "enriched"
    outcome: EnrichmentOutcome


class AnswerReceived(Frozen):
    """A human reply resolved into a decision to commit (SPEC §3)."""

    kind: Literal["answer_received"] = "answer_received"
    decision: Decision


class ClarifyRequested(Frozen):
    """A follow-up question is needed; the thread volleys (SPEC §3)."""

    kind: Literal["clarify_requested"] = "clarify_requested"
    question: str


class PatienceExpired(Frozen):
    """No reply within the patience window; hand off (SPEC §3)."""

    kind: Literal["patience_expired"] = "patience_expired"


class WriteVerified(Frozen):
    """The read-after-write verification for a committed write completed."""

    kind: Literal["write_verified"] = "write_verified"
    outcome: VerifyOutcome


class Converged(Frozen):
    """A REVISING converge-to-target run produced its outcome."""

    kind: Literal["converged"] = "converged"
    outcome: ConvergeOutcome


class ArchiveWindowReached(Frozen):
    """The transaction has been quiet past the archive window (SPEC §3)."""

    kind: Literal["archive_window"] = "archive_window"


class OverrideDetected(Frozen):
    """A re-read at archive time found YNAB recategorized out-of-band (§14.2).

    The owner edited the agent's applied category directly in YNAB rather than
    by replying — a silent correction. ``decision`` carries the human's current
    YNAB state (read back), so the spine demotes the driving rule back to
    Observe and closes the book on the owner's choice.
    """

    kind: Literal["override_detected"] = "override_detected"
    decision: Decision


LifecycleEvent = Annotated[
    SnapshotMaterialized
    | SnapshotUnavailable
    | InboundReceived
    | HoldResolved
    | HoldDeadlineReached
    | Enriched
    | AnswerReceived
    | ClarifyRequested
    | PatienceExpired
    | WriteVerified
    | Converged
    | ArchiveWindowReached
    | OverrideDetected,
    Field(discriminator="kind"),
]
