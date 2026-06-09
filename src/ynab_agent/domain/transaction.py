"""The transaction: a discriminated union over its lifecycle states (SPEC §3).

Rather than one model with a ``state`` field and a pile of optional data (which
admits illegal combinations like "applied, but no decision"), each state is its
own frozen model carrying *exactly* the data valid in that state. mypy then
proves, via ``assert_never`` in the state machine, that every state is handled.

``Discovered`` is special: a transaction can be *born from a signal* before W1
has polled its YNAB snapshot, so ``Discovered`` holds only the id (plus any
buffered signals) and has no snapshot yet. Every other state embeds a
:class:`TxnCore`, whose snapshot is always present.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ynab_agent.domain.allocations import ResolvedSplit, ResolvedSplitLine
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import (
    AwaitingFlag,
    ClearedState,
    DecidedBy,
    FlagColor,
    RevisingOrigin,
    TxnState,
)
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    ImportId,
    PayeeId,
    ReceiptId,
    ThreadId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision, Proposal
from ynab_agent.domain.signals import InboundSignal


class YnabSnapshot(Frozen):
    """A point-in-time read of a YNAB transaction (re-read on each signal).

    Reconciliation is derived, not stored alongside a contradictory flag: a
    transaction is reconciled exactly when ``cleared is RECONCILED``, so the
    illegal "reconciled but uncleared" combination cannot be represented.
    """

    ynab_id: YnabTransactionId
    account: AccountId
    payee: str
    payee_id: PayeeId | None = None
    amount: Money
    txn_date: datetime.date
    posted_at: datetime.datetime | None = None
    memo: str | None = None
    flag: FlagColor | None = None
    category_id: CategoryId | None = None
    cleared: ClearedState = ClearedState.UNCLEARED
    approved: bool = False
    month_closed: bool = False
    import_id: ImportId | None = None
    matched_transaction_id: YnabTransactionId | None = None
    # The split's subtransactions when this is a split parent (``category_id``
    # is then ``None``); empty for a whole-category txn. Carried so a split
    # write can be verified field-by-field on read-back (SPEC §3 r4).
    subtransactions: tuple[ResolvedSplitLine, ...] = ()

    @property
    def reconciled(self) -> bool:
        """Whether YNAB considers this transaction reconciled."""
        return self.cleared is ClearedState.RECONCILED

    @property
    def categorized(self) -> bool:
        """Whether a YNAB category is assigned (LAPSED archive guard, §3)."""
        return self.category_id is not None

    @property
    def has_memo(self) -> bool:
        """Whether the memo is present and non-blank (Amazon-hold exit)."""
        return bool(self.memo and self.memo.strip())


class TxnCore(Frozen):
    """The data common to every post-snapshot state."""

    snapshot: YnabSnapshot
    thread_id: ThreadId | None = None
    receipt_ids: tuple[ReceiptId, ...] = ()
    audit_log_ref: str | None = None


def _ensure_committed(core: TxnCore, decision: Decision) -> None:
    """Enforce the shared invariant of every post-write state (SPEC §3).

    A written state must carry an approved decision, and a split's line amounts
    must sum exactly to the transaction total.

    Raises:
        ValueError: If the decision is unapproved or a split is unbalanced.
    """
    if not decision.approved:
        msg = "a post-write state requires an approved decision"
        raise ValueError(msg)
    allocation = decision.allocation
    if (
        isinstance(allocation, ResolvedSplit)
        and allocation.total != core.snapshot.amount
    ):
        msg = "split lines must sum to the transaction amount"
        raise ValueError(msg)


# ── The lifecycle states ────────────────────────────────────────────────────
class Discovered(Frozen):
    """Born, snapshot possibly not yet polled. Buffers signals until it is."""

    state: Literal[TxnState.DISCOVERED] = TxnState.DISCOVERED
    ynab_id: YnabTransactionId
    thread_id: ThreadId | None = None
    pending: tuple[InboundSignal, ...] = ()


class HoldAmazon(Frozen):
    """An Amazon transaction with an empty memo, held for detail (SPEC §3)."""

    state: Literal[TxnState.HOLD_AMAZON] = TxnState.HOLD_AMAZON
    core: TxnCore
    amazon_deadline: datetime.datetime


class Enriching(Frozen):
    """Assembling the proposal and routing via the autonomy gate."""

    state: Literal[TxnState.ENRICHING] = TxnState.ENRICHING
    core: TxnCore
    proposal: Proposal | None = None


class AutoApplied(Frozen):
    """Written and approved by the agent under a blessed rule; pre-verify."""

    state: Literal[TxnState.AUTO_APPLIED] = TxnState.AUTO_APPLIED
    core: TxnCore
    decision: Decision

    @model_validator(mode="after")
    def _check(self) -> AutoApplied:
        if self.decision.decided_by is not DecidedBy.AGENT:
            msg = "AUTO_APPLIED requires an agent decision"
            raise ValueError(msg)
        _ensure_committed(self.core, self.decision)
        return self


class AwaitingHuman(Frozen):
    """A proposal or question is on the thread; a patience timer runs (§3)."""

    state: Literal[TxnState.AWAITING_HUMAN] = TxnState.AWAITING_HUMAN
    core: TxnCore
    patience_deadline: datetime.datetime
    proposal: Proposal | None = None
    flag: AwaitingFlag = AwaitingFlag.NONE


class Applied(Frozen):
    """Written and approved via the thread (a reply, or a re-applied revision).

    Unlike ``AUTO_APPLIED``, this is reached after thread interaction, so the
    decision may be human- or agent-decided (e.g. a receipt-driven re-apply);
    ``decision.decided_by`` records which.
    """

    state: Literal[TxnState.APPLIED] = TxnState.APPLIED
    core: TxnCore
    decision: Decision

    @model_validator(mode="after")
    def _check(self) -> Applied:
        _ensure_committed(self.core, self.decision)
        return self


class Open(Frozen):
    """The resting state after a verified write. A late inbound reopens it."""

    state: Literal[TxnState.OPEN] = TxnState.OPEN
    core: TxnCore
    decision: Decision

    @model_validator(mode="after")
    def _check(self) -> Open:
        _ensure_committed(self.core, self.decision)
        return self


class Lapsed(Frozen):
    """Patience expired with no answer; handed back, never guessed (SPEC §3)."""

    state: Literal[TxnState.LAPSED] = TxnState.LAPSED
    core: TxnCore
    proposal: Proposal | None = None


class Revising(Frozen):
    """One converge-to-target run; the newest instruction wins (SPEC §3)."""

    state: Literal[TxnState.REVISING] = TxnState.REVISING
    core: TxnCore
    instruction: InboundSignal
    origin: RevisingOrigin
    prior: Decision | None = None


class Archived(Frozen):
    """Terminal: quiet past the window and reconciled (SPEC §3)."""

    state: Literal[TxnState.ARCHIVED] = TxnState.ARCHIVED
    core: TxnCore
    final: Decision | None = None

    @model_validator(mode="after")
    def _check_archivable(self) -> Archived:
        # Archiving is tied to reconciliation; a never-applied (LAPSED) archive
        # additionally requires the txn be categorized, not merely reconciled
        # (YNAB allows reconciling an uncategorized txn) (SPEC §3).
        if not self.core.snapshot.reconciled:
            msg = "ARCHIVED requires a reconciled snapshot"
            raise ValueError(msg)
        if self.final is None and not self.core.snapshot.categorized:
            msg = "an unapplied ARCHIVED requires a categorized snapshot"
            raise ValueError(msg)
        return self


Transaction = Annotated[
    Discovered
    | HoldAmazon
    | Enriching
    | AutoApplied
    | AwaitingHuman
    | Applied
    | Open
    | Lapsed
    | Revising
    | Archived,
    Field(discriminator="state"),
]


def born(
    ynab_id: YnabTransactionId, thread_id: ThreadId | None = None
) -> Discovered:
    """Create a freshly discovered transaction (snapshot not yet required)."""
    return Discovered(ynab_id=ynab_id, thread_id=thread_id)
