"""Tests for the transaction lifecycle state machine (SPEC §3)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import ProposedCategory, ResolvedCategory
from ynab_agent.domain.config import DEFAULT_POLICY
from ynab_agent.domain.effects import (
    CancelTimer,
    CloseThread,
    CommitToYnab,
    Effect,
    FeedRuleLearning,
    MessagePurpose,
    OpenThread,
    RecordAutoAction,
    ReplayBuffered,
    RuleLearningKind,
    SendThreadMessage,
    SetTimer,
    TimerKind,
)
from ynab_agent.domain.enums import (
    AwaitingFlag,
    ClearedState,
    Confidence,
    DecidedBy,
    RevisingOrigin,
)
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
    OverrideDetected,
    PatienceExpired,
    Reapplied,
    SnapshotMaterialized,
    SnapshotUnavailable,
    VerifyOutcome,
    WriteVerified,
)
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    MessageId,
    RuleId,
    ThreadId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision, Proposal
from ynab_agent.domain.signals import ReplySignal
from ynab_agent.domain.state_machine import Transition, TransitionKind, advance
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
    YnabSnapshot,
    born,
)

NOW = datetime.datetime(2026, 5, 28, 12, 0, tzinfo=datetime.UTC)
POLICY = DEFAULT_POLICY


# ── builders ────────────────────────────────────────────────────────────────
def _snapshot(**kw: object) -> YnabSnapshot:
    base: dict[str, object] = {
        "ynab_id": YnabTransactionId("t1"),
        "account": AccountId("a1"),
        "payee": "Blue Bottle",
        "amount": Money.from_currency("-4.50"),
        "txn_date": datetime.date(2026, 5, 28),
    }
    base.update(kw)
    return YnabSnapshot.model_validate(base)


def _core(thread: bool = True, **snap: object) -> TxnCore:
    return TxnCore(
        snapshot=_snapshot(**snap),
        thread_id=ThreadId("thr1") if thread else None,
    )


def _decision(
    by: DecidedBy = DecidedBy.AGENT, category: str = "dining"
) -> Decision:
    return Decision(
        allocation=ResolvedCategory(category=CategoryId(category)),
        approved=True,
        decided_by=by,
        decided_at=NOW,
    )


def _proposal() -> Proposal:
    return Proposal(
        allocation=ProposedCategory(category=CategoryId("dining")),
        confidence=Confidence.HIGH,
        rationale="merchant known",
    )


def _reply() -> ReplySignal:
    return ReplySignal(
        thread_id=ThreadId("thr1"),
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        text="ok",
    )


def _step(txn: Transaction, event: LifecycleEvent) -> Transition:
    return advance(txn, event, now=NOW, policy=POLICY)


def _of(transition: Transition, cls: type[Effect]) -> list[Effect]:
    return [e for e in transition.effects if isinstance(e, cls)]


def _timers(transition: Transition, kind: TimerKind) -> list[SetTimer]:
    return [
        e
        for e in transition.effects
        if isinstance(e, SetTimer) and e.timer is kind
    ]


def _purposes(transition: Transition) -> set[MessagePurpose]:
    return {
        e.purpose
        for e in transition.effects
        if isinstance(e, SendThreadMessage)
    }


def _cancels(transition: Transition, kind: TimerKind) -> bool:
    return any(
        isinstance(e, CancelTimer) and e.timer is kind
        for e in transition.effects
    )


# ── DISCOVERED ──────────────────────────────────────────────────────────────
def test_discovered_to_enriching() -> None:
    out = _step(
        born(YnabTransactionId("t1")),
        SnapshotMaterialized(snapshot=_snapshot()),
    )
    assert isinstance(out.next, Enriching)


def test_discovered_to_hold_amazon_sets_timer() -> None:
    out = _step(
        born(YnabTransactionId("t1")),
        SnapshotMaterialized(
            snapshot=_snapshot(payee="Amazon"), hold_for_amazon=True
        ),
    )
    assert isinstance(out.next, HoldAmazon)
    assert out.next.amazon_deadline == NOW + POLICY.amazon_hold
    assert _timers(out, TimerKind.AMAZON_HOLD)


def test_discovered_buffers_inbound_and_adopts_thread() -> None:
    out = _step(born(YnabTransactionId("t1")), InboundReceived(signal=_reply()))
    assert isinstance(out.next, Discovered)
    assert len(out.next.pending) == 1
    assert out.next.thread_id == ThreadId("thr1")


def test_discovered_replays_buffered_on_materialize() -> None:
    buffered = Discovered(ynab_id=YnabTransactionId("t1"), pending=(_reply(),))
    out = _step(buffered, SnapshotMaterialized(snapshot=_snapshot()))
    assert isinstance(out.next, Enriching)
    assert _of(out, ReplayBuffered)


def test_discovered_snapshot_unavailable_is_ignored() -> None:
    out = _step(born(YnabTransactionId("t1")), SnapshotUnavailable())
    assert out.kind is TransitionKind.IGNORED
    assert isinstance(out.next, Discovered)


# ── HOLD_AMAZON ─────────────────────────────────────────────────────────────
def test_hold_resolved_resumes_enriching() -> None:
    held = HoldAmazon(core=_core(), amazon_deadline=NOW)
    out = _step(held, HoldResolved(snapshot=_snapshot(memo="cable")))
    assert isinstance(out.next, Enriching)
    assert out.next.core.snapshot.memo == "cable"
    assert _of(out, CancelTimer)


def test_hold_deadline_falls_back_to_enriching() -> None:
    held = HoldAmazon(core=_core(), amazon_deadline=NOW)
    out = _step(held, HoldDeadlineReached())
    assert isinstance(out.next, Enriching)


def test_hold_receipt_short_circuits() -> None:
    held = HoldAmazon(core=_core(), amazon_deadline=NOW)
    out = _step(held, InboundReceived(signal=_reply()))
    assert isinstance(out.next, Enriching)
    assert _of(out, ReplayBuffered)


# ── ENRICHING ───────────────────────────────────────────────────────────────
def test_enriched_auto_apply_commits() -> None:
    out = _step(
        Enriching(core=_core()),
        Enriched(outcome=AutoApply(decision=_decision(DecidedBy.AGENT))),
    )
    assert isinstance(out.next, AutoApplied)
    assert _of(out, CommitToYnab)
    # The auto-action is recorded in the circuit-breaker ledger (SPEC §0.6) so
    # the per-run/per-day floor counts are real and can trip.
    records = _of(out, RecordAutoAction)
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, RecordAutoAction)
    assert record.ynab_id == "t1"


def test_enriched_ask_human_opens_and_times() -> None:
    # First contact (no thread yet): open the thread — which sends the proposal
    # as its first email — and start the patience timer. No SEPARATE proposal
    # send is emitted; open_thread carries it (a thread starts on its send).
    out = _step(
        Enriching(core=_core(thread=False)),
        Enriched(outcome=AskHuman(proposal=_proposal())),
    )
    assert isinstance(out.next, AwaitingHuman)
    assert out.next.patience_deadline == NOW + POLICY.patience_window
    assert _of(out, OpenThread)
    assert _timers(out, TimerKind.PATIENCE)
    assert MessagePurpose.PROPOSAL not in _purposes(out)


def test_enriched_ask_human_with_open_thread_sends_proposal_reply() -> None:
    # The thread is already open (a re-proposal): send the proposal as a reply,
    # and do NOT re-open the thread.
    out = _step(
        Enriching(core=_core(thread=True)),
        Enriched(outcome=AskHuman(proposal=_proposal())),
    )
    assert isinstance(out.next, AwaitingHuman)
    assert not _of(out, OpenThread)
    assert MessagePurpose.PROPOSAL in _purposes(out)
    assert _timers(out, TimerKind.PATIENCE)


# ── AUTO_APPLIED / APPLIED verify ───────────────────────────────────────────
def test_auto_applied_verify_match_opens_with_fyi() -> None:
    aa = AutoApplied(core=_core(), decision=_decision(DecidedBy.AGENT))
    out = _step(aa, WriteVerified(outcome=VerifyOutcome.MATCH))
    assert isinstance(out.next, Open)
    assert _timers(out, TimerKind.ARCHIVE)
    assert not _of(out, FeedRuleLearning)  # auto-apply teaches nothing new


def test_applied_verify_match_confirms_and_learns() -> None:
    ap = Applied(core=_core(), decision=_decision(DecidedBy.HUMAN))
    out = _step(ap, WriteVerified(outcome=VerifyOutcome.MATCH))
    assert isinstance(out.next, Open)
    learn = _of(out, FeedRuleLearning)
    assert learn and isinstance(learn[0], FeedRuleLearning)
    assert learn[0].event is RuleLearningKind.CONFIRM


def test_verify_could_not_confirm_flags_awaiting() -> None:
    aa = AutoApplied(core=_core(), decision=_decision(DecidedBy.AGENT))
    out = _step(aa, WriteVerified(outcome=VerifyOutcome.COULD_NOT_CONFIRM))
    assert isinstance(out.next, AwaitingHuman)
    assert out.next.flag is AwaitingFlag.POSSIBLY_INCONSISTENT


def test_verify_diverged_flags_awaiting() -> None:
    ap = Applied(core=_core(), decision=_decision(DecidedBy.HUMAN))
    out = _step(ap, WriteVerified(outcome=VerifyOutcome.DIVERGED))
    assert isinstance(out.next, AwaitingHuman)
    assert out.next.flag is AwaitingFlag.DIVERGED


# ── AWAITING_HUMAN ──────────────────────────────────────────────────────────
def _awaiting(flag: AwaitingFlag = AwaitingFlag.NONE) -> AwaitingHuman:
    return AwaitingHuman(
        core=_core(),
        proposal=_proposal() if flag is AwaitingFlag.NONE else None,
        patience_deadline=NOW,
        flag=flag,
    )


def test_answer_received_applies_and_commits() -> None:
    out = _step(
        _awaiting(), AnswerReceived(decision=_decision(DecidedBy.HUMAN))
    )
    assert isinstance(out.next, Applied)
    assert _of(out, CommitToYnab)
    assert _cancels(out, TimerKind.PATIENCE)


def test_clarify_keeps_waiting_and_rearms() -> None:
    out = _step(_awaiting(), ClarifyRequested(question="which card?"))
    assert isinstance(out.next, AwaitingHuman)
    assert _timers(out, TimerKind.PATIENCE)


def test_patience_expired_lapses_with_handoff() -> None:
    out = _step(_awaiting(), PatienceExpired())
    assert isinstance(out.next, Lapsed)
    assert MessagePurpose.HANDOFF in _purposes(out)


def test_flagged_awaiting_does_not_lapse() -> None:
    out = _step(
        _awaiting(AwaitingFlag.POSSIBLY_INCONSISTENT), PatienceExpired()
    )
    assert out.kind is TransitionKind.IGNORED
    assert isinstance(out.next, AwaitingHuman)


# ── OPEN ────────────────────────────────────────────────────────────────────
def test_open_inbound_revises() -> None:
    opened = Open(core=_core(), decision=_decision(DecidedBy.HUMAN))
    out = _step(opened, InboundReceived(signal=_reply()))
    assert isinstance(out.next, Revising)
    assert out.next.origin is RevisingOrigin.APPLIED
    assert out.next.prior is not None
    assert any(
        isinstance(e, CancelTimer) and e.timer is TimerKind.ARCHIVE
        for e in out.effects
    )


def test_open_archives_when_reconciled() -> None:
    opened = Open(
        core=_core(cleared=ClearedState.RECONCILED),
        decision=_decision(DecidedBy.HUMAN),
    )
    out = _step(opened, ArchiveWindowReached())
    assert isinstance(out.next, Archived)
    assert _of(out, CloseThread)


def test_open_archive_blocked_when_unreconciled() -> None:
    opened = Open(core=_core(), decision=_decision(DecidedBy.HUMAN))
    out = _step(opened, ArchiveWindowReached())
    assert out.kind is TransitionKind.IGNORED


def _agent_rule_decision() -> Decision:
    return _decision(DecidedBy.AGENT, category="dining").model_copy(
        update={"rule_id": RuleId("r1")}
    )


def test_open_override_detected_demotes_and_archives_when_reconciled() -> None:
    # The agent auto-applied "dining" via rule r1; the owner recategorized it to
    # "groceries" directly in YNAB (a silent correction). The spine demotes the
    # driving rule and closes on the owner's choice (SPEC §14.2).
    agent = _agent_rule_decision()
    opened = Open(core=_core(cleared=ClearedState.RECONCILED), decision=agent)
    human = _decision(DecidedBy.HUMAN, category="groceries")
    out = _step(opened, OverrideDetected(decision=human))
    assert isinstance(out.next, Archived)
    assert out.next.final == human
    learn = next(e for e in out.effects if isinstance(e, FeedRuleLearning))
    assert learn.event is RuleLearningKind.CORRECT
    assert learn.prior == agent  # demotes the *driving* rule (carries rule_id)
    assert learn.decision == human
    assert any(
        isinstance(e, SendThreadMessage)
        and e.purpose is MessagePurpose.OVERRIDE_NOTICE
        for e in out.effects
    )
    assert _of(out, CloseThread)


def test_open_override_detected_demotes_without_archiving_unreconciled() -> (
    None
):
    # Same silent edit, but the txn is not reconciled yet: still demote and
    # notify, adopt the owner's value, and stay OPEN (ARCHIVED needs that).
    agent = _agent_rule_decision()
    opened = Open(core=_core(), decision=agent)  # default: not reconciled
    human = _decision(DecidedBy.HUMAN, category="groceries")
    out = _step(opened, OverrideDetected(decision=human))
    assert isinstance(out.next, Open)
    assert out.next.decision == human  # adopted reality
    learn = next(e for e in out.effects if isinstance(e, FeedRuleLearning))
    assert learn.event is RuleLearningKind.CORRECT
    assert not _of(out, CloseThread)


# ── LAPSED ──────────────────────────────────────────────────────────────────
def test_lapsed_inbound_reopens_from_lapsed() -> None:
    lapsed = Lapsed(core=_core(), proposal=_proposal())
    out = _step(lapsed, InboundReceived(signal=_reply()))
    assert isinstance(out.next, Revising)
    assert out.next.origin is RevisingOrigin.LAPSED
    assert out.next.prior is None


def test_lapsed_archive_requires_categorized() -> None:
    lapsed = Lapsed(
        core=_core(cleared=ClearedState.RECONCILED),  # reconciled, no category
        proposal=_proposal(),
    )
    out = _step(lapsed, ArchiveWindowReached())
    # Not categorized: warn rather than archive silently.
    assert isinstance(out.next, Lapsed)
    assert MessagePurpose.ARCHIVE_NOTICE in _purposes(out)


def test_lapsed_archive_when_reconciled_and_categorized() -> None:
    lapsed = Lapsed(
        core=_core(
            cleared=ClearedState.RECONCILED, category_id=CategoryId("dining")
        ),
        proposal=_proposal(),
    )
    out = _step(lapsed, ArchiveWindowReached())
    assert isinstance(out.next, Archived)


# ── REVISING ────────────────────────────────────────────────────────────────
def _revising(origin: RevisingOrigin, *, prior: bool) -> Revising:
    return Revising(
        core=_core(),
        instruction=_reply(),
        origin=origin,
        prior=_decision(DecidedBy.HUMAN) if prior else None,
    )


def test_revising_reapplied_category_change_demotes_prior_rule() -> None:
    # prior "dining" → revised to "gifts": a real correction.
    rev = _revising(RevisingOrigin.APPLIED, prior=True)
    new = _decision(DecidedBy.HUMAN, category="gifts")
    out = _step(rev, Converged(outcome=Reapplied(decision=new)))
    assert isinstance(out.next, Open)
    learn = _of(out, FeedRuleLearning)
    assert learn and isinstance(learn[0], FeedRuleLearning)
    assert learn[0].event is RuleLearningKind.CORRECT
    assert learn[0].prior is not None  # the rule to demote travels with it


def test_revising_reapplied_same_category_confirms() -> None:
    # Same category (e.g. a receipt adding a memo) confirms, never demotes.
    rev = _revising(RevisingOrigin.APPLIED, prior=True)
    out = _step(
        rev, Converged(outcome=Reapplied(decision=_decision(DecidedBy.HUMAN)))
    )
    learn = _of(out, FeedRuleLearning)
    assert learn and isinstance(learn[0], FeedRuleLearning)
    assert learn[0].event is RuleLearningKind.CONFIRM


def test_revising_accepts_mid_cycle_inbound() -> None:
    # A correction landing mid-converge is retained (newest wins), not dropped.
    rev = _revising(RevisingOrigin.APPLIED, prior=True)
    out = _step(rev, InboundReceived(signal=_reply()))
    assert out.kind is TransitionKind.ADVANCED
    assert isinstance(out.next, Revising)
    assert _of(out, ReplayBuffered)


def test_revising_no_change_from_applied_returns_open() -> None:
    rev = _revising(RevisingOrigin.APPLIED, prior=True)
    out = _step(rev, Converged(outcome=NoChange()))
    assert isinstance(out.next, Open)


def test_revising_no_change_from_lapsed_rearms_awaiting() -> None:
    rev = _revising(RevisingOrigin.LAPSED, prior=False)
    out = _step(rev, Converged(outcome=NoChange()))
    assert isinstance(out.next, AwaitingHuman)
    assert _timers(out, TimerKind.PATIENCE)


def test_revising_could_not_confirm_flags() -> None:
    rev = _revising(RevisingOrigin.APPLIED, prior=True)
    out = _step(rev, Converged(outcome=CouldNotConfirm()))
    assert isinstance(out.next, AwaitingHuman)
    assert out.next.flag is AwaitingFlag.POSSIBLY_INCONSISTENT


def test_revising_diverged_reads_back() -> None:
    rev = _revising(RevisingOrigin.APPLIED, prior=True)
    out = _step(
        rev,
        Converged(outcome=Diverged(ynab_summary="X", requested_summary="Y")),
    )
    assert isinstance(out.next, AwaitingHuman)
    assert out.next.flag is AwaitingFlag.DIVERGED


def test_revising_needs_human_asks() -> None:
    rev = _revising(RevisingOrigin.APPLIED, prior=True)
    out = _step(rev, Converged(outcome=NeedsHuman(reason="reconciled")))
    assert isinstance(out.next, AwaitingHuman)


# ── ARCHIVED + rejections ───────────────────────────────────────────────────
def test_archived_rejects_events() -> None:
    archived = Archived(
        core=_core(
            cleared=ClearedState.RECONCILED, category_id=CategoryId("dining")
        )
    )
    out = _step(archived, InboundReceived(signal=_reply()))
    assert out.kind is TransitionKind.REJECTED


def test_unexpected_event_is_rejected() -> None:
    opened = Open(core=_core(), decision=_decision(DecidedBy.HUMAN))
    out = _step(opened, Enriched(outcome=AskHuman(proposal=_proposal())))
    assert out.kind is TransitionKind.REJECTED
