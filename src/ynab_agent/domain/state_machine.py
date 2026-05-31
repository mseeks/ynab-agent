"""The transaction lifecycle state machine (SPEC §3).

``advance(txn, event, now, policy)`` is a pure function: given the current
transaction, a decided event, the current time, and the timer policy, it returns
a :class:`Transition` — the next transaction plus the effects the spine should
execute. It performs no I/O and reads no clock; ``now`` is passed in, so replay
is deterministic.

Dispatch is per-state (one handler each), and ``advance`` ends in
``assert_never`` so mypy proves all ten states are handled. An event a state
does not expect yields a ``REJECTED`` transition (no state change), never an
exception, so the spine decides how to handle the surprise.

Interpretation notes (where the SPEC's diagram is collapsed for a pure model):

* The REVISING converge step already bundles commit *and* verify (SPEC §3 rules
  3-4), so its ``Reapplied`` outcome lands directly in ``OPEN`` (re-approved and
  verified), collapsing the degenerate ``APPLIED → OPEN`` hop the diagram draws.
* ``AWAITING_HUMAN`` reached from a verify failure carries a ``flag`` and no
  proposal; per the SPEC such flagged entries do *not* lapse into the generic
  hand-off note, so ``PatienceExpired`` there is ignored (the §13 sweep tracks
  it).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.effects import (
    CancelTimer,
    CloseThread,
    CommitToYnab,
    Effect,
    FeedRuleLearning,
    MessagePurpose,
    OpenThread,
    ReplayBuffered,
    RuleLearningKind,
    SendThreadMessage,
    SetTimer,
    TimerKind,
)
from ynab_agent.domain.enums import AwaitingFlag, RevisingOrigin
from ynab_agent.domain.events import (
    AnswerReceived,
    ArchiveWindowReached,
    AskHuman,
    AutoApply,
    ClarifyRequested,
    Converged,
    CouldNotConfirm,
    Diverged,
    Enriched,
    HoldDeadlineReached,
    HoldResolved,
    InboundReceived,
    LifecycleEvent,
    NeedsHuman,
    NoChange,
    PatienceExpired,
    Reapplied,
    SnapshotMaterialized,
    SnapshotUnavailable,
    VerifyOutcome,
    WriteVerified,
)
from ynab_agent.domain.signals import ReplySignal
from ynab_agent.domain.transaction import (
    Applied,
    Archived,
    AutoApplied,
    AwaitingHuman,
    Discovered,
    Enriching,
    HoldAmazon,
    Lapsed,
    Open,
    Revising,
    Transaction,
    TxnCore,
)

if TYPE_CHECKING:
    import datetime

    from ynab_agent.domain.config import LifecyclePolicy
    from ynab_agent.domain.proposal import Decision


class TransitionKind(StrEnum):
    """The disposition of an ``advance`` call."""

    ADVANCED = "advanced"
    IGNORED = "ignored"
    REJECTED = "rejected"


class Transition(Frozen):
    """The result of advancing a transaction.

    Attributes:
        kind: ``ADVANCED`` (state and/or effects), ``IGNORED`` (legal no-op), or
            ``REJECTED`` (the event is not valid in this state).
        next: The resulting transaction (unchanged for IGNORED/REJECTED).
        effects: The effects the spine should execute, in order.
        reason: A short explanation for IGNORED/REJECTED.
    """

    kind: TransitionKind
    next: Transaction
    effects: tuple[Effect, ...] = ()
    reason: str | None = None


def _advanced(nxt: Transaction, *effects: Effect) -> Transition:
    return Transition(kind=TransitionKind.ADVANCED, next=nxt, effects=effects)


def _ignored(txn: Transaction, reason: str) -> Transition:
    return Transition(kind=TransitionKind.IGNORED, next=txn, reason=reason)


def _rejected(txn: Transaction, event: LifecycleEvent) -> Transition:
    reason = f"{type(event).__name__} is not valid in {txn.state}"
    return Transition(kind=TransitionKind.REJECTED, next=txn, reason=reason)


def advance(
    txn: Transaction,
    event: LifecycleEvent,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    """Advance a transaction by one event. Pure; see the module docstring."""
    match txn:
        case Discovered():
            return _from_discovered(txn, event, now=now, policy=policy)
        case HoldAmazon():
            return _from_hold_amazon(txn, event)
        case Enriching():
            return _from_enriching(txn, event, now=now, policy=policy)
        case AutoApplied():
            return _from_auto_applied(txn, event, now=now, policy=policy)
        case AwaitingHuman():
            return _from_awaiting_human(txn, event, now=now, policy=policy)
        case Applied():
            return _from_applied(txn, event, now=now, policy=policy)
        case Open():
            return _from_open(txn, event)
        case Lapsed():
            return _from_lapsed(txn, event)
        case Revising():
            return _from_revising(txn, event, now=now, policy=policy)
        case Archived():
            return _from_archived(txn, event)
    assert_never(txn)


def _from_discovered(
    txn: Discovered,
    event: LifecycleEvent,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    match event:
        case SnapshotMaterialized(snapshot=snap, hold_for_amazon=hold):
            core = TxnCore(snapshot=snap, thread_id=txn.thread_id)
            replay: tuple[Effect, ...] = (
                (ReplayBuffered(signals=txn.pending),) if txn.pending else ()
            )
            if hold:
                deadline = now + policy.amazon_hold
                return _advanced(
                    HoldAmazon(core=core, amazon_deadline=deadline),
                    SetTimer(timer=TimerKind.AMAZON_HOLD, deadline=deadline),
                    *replay,
                )
            return _advanced(Enriching(core=core), *replay)
        case SnapshotUnavailable():
            return _ignored(txn, "snapshot not yet in YNAB; staying DISCOVERED")
        case InboundReceived(signal=sig):
            thread_id = txn.thread_id
            if thread_id is None and isinstance(sig, ReplySignal):
                thread_id = sig.thread_id
            buffered = Discovered(
                ynab_id=txn.ynab_id,
                thread_id=thread_id,
                pending=(*txn.pending, sig),
            )
            return _advanced(buffered)
        case _:
            return _rejected(txn, event)


def _from_hold_amazon(txn: HoldAmazon, event: LifecycleEvent) -> Transition:
    match event:
        case HoldResolved(snapshot=snap):
            core = txn.core.model_copy(update={"snapshot": snap})
            return _advanced(
                Enriching(core=core),
                CancelTimer(timer=TimerKind.AMAZON_HOLD),
            )
        case HoldDeadlineReached():
            # Deadline hit: enrich and fall back to asking (SPEC §3).
            return _advanced(Enriching(core=txn.core))
        case InboundReceived(signal=sig):
            # A matched receipt (or reply) short-circuits the hold (SPEC §6).
            return _advanced(
                Enriching(core=txn.core),
                CancelTimer(timer=TimerKind.AMAZON_HOLD),
                ReplayBuffered(signals=(sig,)),
            )
        case _:
            return _rejected(txn, event)


def _from_enriching(
    txn: Enriching,
    event: LifecycleEvent,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    match event:
        case Enriched(outcome=outcome):
            match outcome:
                case AutoApply(decision=decision):
                    return _advanced(
                        AutoApplied(core=txn.core, decision=decision),
                        CommitToYnab(decision=decision),
                    )
                case AskHuman(proposal=proposal):
                    deadline = now + policy.patience_window
                    effects: tuple[Effect, ...] = ()
                    if txn.core.thread_id is None:
                        effects = (OpenThread(),)
                    return _advanced(
                        AwaitingHuman(
                            core=txn.core,
                            proposal=proposal,
                            patience_deadline=deadline,
                        ),
                        *effects,
                        SendThreadMessage(purpose=MessagePurpose.PROPOSAL),
                        SetTimer(timer=TimerKind.PATIENCE, deadline=deadline),
                    )
            assert_never(outcome)
        case InboundReceived(signal=sig):
            # Re-feed so enrichment incorporates the new signal; stay ENRICHING.
            return Transition(
                kind=TransitionKind.ADVANCED,
                next=txn,
                effects=(ReplayBuffered(signals=(sig,)),),
            )
        case _:
            return _rejected(txn, event)


def _from_auto_applied(
    txn: AutoApplied,
    event: LifecycleEvent,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    match event:
        case WriteVerified(outcome=outcome):
            return _resolve_write_verify(
                core=txn.core,
                decision=txn.decision,
                outcome=outcome,
                applied_message=MessagePurpose.FYI,
                learning=None,
                now=now,
                policy=policy,
            )
        case _:
            return _rejected(txn, event)


def _from_applied(
    txn: Applied,
    event: LifecycleEvent,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    match event:
        case WriteVerified(outcome=outcome):
            return _resolve_write_verify(
                core=txn.core,
                decision=txn.decision,
                outcome=outcome,
                applied_message=MessagePurpose.CONFIRM,
                learning=RuleLearningKind.CONFIRM,
                now=now,
                policy=policy,
            )
        case _:
            return _rejected(txn, event)


def _resolve_write_verify(
    *,
    core: TxnCore,
    decision: Decision,
    outcome: VerifyOutcome,
    applied_message: MessagePurpose,
    learning: RuleLearningKind | None,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    """Shared AUTO_APPLIED/APPLIED → OPEN-or-AWAITING handling (SPEC §3)."""
    match outcome:
        case VerifyOutcome.MATCH:
            archive = now + policy.archive_window
            effects: tuple[Effect, ...] = (
                SendThreadMessage(purpose=applied_message),
                SetTimer(timer=TimerKind.ARCHIVE, deadline=archive),
            )
            if learning is not None:
                effects = (
                    FeedRuleLearning(
                        event=learning,
                        payee=core.snapshot.payee,
                        decision=decision,
                    ),
                    *effects,
                )
            return _advanced(Open(core=core, decision=decision), *effects)
        case VerifyOutcome.COULD_NOT_CONFIRM:
            return _enter_flagged_awaiting(
                core,
                AwaitingFlag.POSSIBLY_INCONSISTENT,
                MessagePurpose.POSSIBLY_INCONSISTENT,
                now=now,
                policy=policy,
            )
        case VerifyOutcome.DIVERGED:
            return _enter_flagged_awaiting(
                core,
                AwaitingFlag.DIVERGED,
                MessagePurpose.DIVERGED_READBACK,
                now=now,
                policy=policy,
            )
    assert_never(outcome)


def _enter_flagged_awaiting(
    core: TxnCore,
    flag: AwaitingFlag,
    message: MessagePurpose,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    """Route a verify failure to a flagged AWAITING_HUMAN with a read-back."""
    deadline = now + policy.patience_window
    return _advanced(
        AwaitingHuman(core=core, patience_deadline=deadline, flag=flag),
        SendThreadMessage(purpose=message),
        SetTimer(timer=TimerKind.PATIENCE, deadline=deadline),
    )


def _from_awaiting_human(
    txn: AwaitingHuman,
    event: LifecycleEvent,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    match event:
        case AnswerReceived(decision=decision):
            return _advanced(
                Applied(core=txn.core, decision=decision),
                CommitToYnab(decision=decision),
                CancelTimer(timer=TimerKind.PATIENCE),
            )
        case ClarifyRequested():
            deadline = now + policy.patience_window
            return _advanced(
                AwaitingHuman(
                    core=txn.core,
                    proposal=txn.proposal,
                    patience_deadline=deadline,
                    flag=txn.flag,
                ),
                SendThreadMessage(purpose=MessagePurpose.CLARIFY),
                SetTimer(timer=TimerKind.PATIENCE, deadline=deadline),
            )
        case PatienceExpired():
            if txn.flag is not AwaitingFlag.NONE:
                # Flagged (inconsistent/diverged) entries do not generic-lapse.
                return _ignored(txn, "flagged entry does not lapse (SPEC §3)")
            archive = now + policy.archive_window
            return _advanced(
                Lapsed(core=txn.core, proposal=txn.proposal),
                SendThreadMessage(purpose=MessagePurpose.HANDOFF),
                SetTimer(timer=TimerKind.ARCHIVE, deadline=archive),
            )
        case InboundReceived(signal=sig):
            # Re-feed for interpretation into an answer/clarify; keep waiting.
            return Transition(
                kind=TransitionKind.ADVANCED,
                next=txn,
                effects=(ReplayBuffered(signals=(sig,)),),
            )
        case _:
            return _rejected(txn, event)


def _from_open(txn: Open, event: LifecycleEvent) -> Transition:
    match event:
        case InboundReceived(signal=sig):
            return _advanced(
                Revising(
                    core=txn.core,
                    instruction=sig,
                    origin=RevisingOrigin.APPLIED,
                    prior=txn.decision,
                ),
                CancelTimer(timer=TimerKind.ARCHIVE),
            )
        case ArchiveWindowReached():
            if not txn.core.snapshot.reconciled:
                return _ignored(txn, "not reconciled yet; staying OPEN")
            return _advanced(
                Archived(core=txn.core, final=txn.decision),
                CloseThread(),
            )
        case _:
            return _rejected(txn, event)


def _from_lapsed(txn: Lapsed, event: LifecycleEvent) -> Transition:
    match event:
        case InboundReceived(signal=sig):
            return _advanced(
                Revising(
                    core=txn.core,
                    instruction=sig,
                    origin=RevisingOrigin.LAPSED,
                ),
                CancelTimer(timer=TimerKind.ARCHIVE),
            )
        case ArchiveWindowReached():
            snap = txn.core.snapshot
            if not snap.reconciled:
                return _ignored(txn, "not reconciled yet; staying LAPSED")
            if not snap.categorized:
                # Don't go silent: warn, then let the §13 sweep keep tracking.
                return Transition(
                    kind=TransitionKind.ADVANCED,
                    next=txn,
                    effects=(
                        SendThreadMessage(
                            purpose=MessagePurpose.ARCHIVE_NOTICE
                        ),
                    ),
                    reason="uncategorized; warned before archiving (SPEC §3)",
                )
            return _advanced(
                Archived(core=txn.core),
                CloseThread(),
            )
        case _:
            return _rejected(txn, event)


def _from_revising(
    txn: Revising,
    event: LifecycleEvent,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    match event:
        case Converged(outcome=outcome):
            match outcome:
                case Reapplied(decision=decision):
                    return _reapply(txn, decision, now=now, policy=policy)
                case NoChange():
                    return _resolve_no_change(txn, now=now, policy=policy)
                case CouldNotConfirm():
                    return _enter_flagged_awaiting(
                        txn.core,
                        AwaitingFlag.POSSIBLY_INCONSISTENT,
                        MessagePurpose.POSSIBLY_INCONSISTENT,
                        now=now,
                        policy=policy,
                    )
                case Diverged():
                    return _enter_flagged_awaiting(
                        txn.core,
                        AwaitingFlag.DIVERGED,
                        MessagePurpose.DIVERGED_READBACK,
                        now=now,
                        policy=policy,
                    )
                case NeedsHuman():
                    deadline = now + policy.patience_window
                    return _advanced(
                        AwaitingHuman(
                            core=txn.core, patience_deadline=deadline
                        ),
                        SendThreadMessage(purpose=MessagePurpose.PROPOSAL),
                        SetTimer(timer=TimerKind.PATIENCE, deadline=deadline),
                    )
            assert_never(outcome)
        case InboundReceived(signal=sig):
            # Newest instruction wins (SPEC §3 rule 1): retain the correction
            # and re-target the in-flight converge, rather than dropping it.
            return Transition(
                kind=TransitionKind.ADVANCED,
                next=txn.model_copy(update={"instruction": sig}),
                effects=(ReplayBuffered(signals=(sig,)),),
            )
        case _:
            return _rejected(txn, event)


def _reapply(
    txn: Revising,
    decision: Decision,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    """Re-applied revision: open, summarize, and feed learning (SPEC §3, §9).

    A real category change demotes the prior rule (CORRECT, carrying the prior
    decision so W5 demotes the *right* rule); a same-category revision — a
    receipt adding a memo, or a late first answer from LAPSED — only confirms.
    """
    archive = now + policy.archive_window
    is_correction = (
        txn.prior is not None and txn.prior.allocation != decision.allocation
    )
    payee = txn.core.snapshot.payee
    if is_correction:
        learning = FeedRuleLearning(
            event=RuleLearningKind.CORRECT,
            payee=payee,
            decision=decision,
            prior=txn.prior,
        )
    else:
        learning = FeedRuleLearning(
            event=RuleLearningKind.CONFIRM, payee=payee, decision=decision
        )
    return _advanced(
        Open(core=txn.core, decision=decision),
        learning,
        SendThreadMessage(purpose=MessagePurpose.REVISE_SUMMARY),
        SetTimer(timer=TimerKind.ARCHIVE, deadline=archive),
    )


def _resolve_no_change(
    txn: Revising,
    *,
    now: datetime.datetime,
    policy: LifecyclePolicy,
) -> Transition:
    """No-change exit depends on history (SPEC §3 rule 5).

    Already applied → OPEN (resting). Entered from LAPSED (never applied) →
    AWAITING_HUMAN, re-arming patience, so an unhandled txn is not mislabeled.
    """
    if txn.origin is RevisingOrigin.APPLIED and txn.prior is not None:
        archive = now + policy.archive_window
        return _advanced(
            Open(core=txn.core, decision=txn.prior),
            SetTimer(timer=TimerKind.ARCHIVE, deadline=archive),
        )
    deadline = now + policy.patience_window
    return _advanced(
        AwaitingHuman(core=txn.core, patience_deadline=deadline),
        SetTimer(timer=TimerKind.PATIENCE, deadline=deadline),
    )


def _from_archived(txn: Archived, event: LifecycleEvent) -> Transition:
    # Terminal. A late edit is handled by signal-with-start re-instantiating a
    # fresh REVISING run under the same logical id, not by a transition here.
    return _rejected(txn, event)
