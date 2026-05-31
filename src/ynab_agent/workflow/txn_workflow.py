"""W2 · the Transaction Lifecycle workflow (SPEC §3, §0.5).

One durable workflow per ``ynab_id``. It is a thin *driver* around the pure
:func:`~ynab_agent.domain.state_machine.advance` core: each step produces one
event — from an activity (fetch / enrich / commit+verify / converge) or from a
signal or an absolute-deadline timer — feeds it to ``advance``, then dispatches
the emitted effects back out through activities. All nondeterminism lives in
activities; the workflow uses only pure state and Temporal APIs
(``workflow.now`` for time), so it replays deterministically. Long-lived
transactions survive via ``continue-as-new`` from a resting state, carrying
their state forward.

Deferred (a cohesive subsystem for its own step, SPEC §3, §9): the externalized,
append-only **audit log**. ``TxnCore.audit_log_ref`` and the
``_action_seq`` outbound-dedup key are plumbed here, but no audit entries are
written yet — Temporal's own event history provides replay-safety in the
meantime, and ``_action_seq`` is the idempotency key the real send activity will
use so a retry never double-emails.
"""

from __future__ import annotations

import datetime
from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING

from temporalio import workflow

if TYPE_CHECKING:
    from collections.abc import Callable

with workflow.unsafe.imports_passed_through():
    from ynab_agent.domain.config import DEFAULT_POLICY
    from ynab_agent.domain.effects import (
        CancelTimer,
        CloseThread,
        CommitToYnab,
        Effect,
        FeedRuleLearning,
        OpenThread,
        ReplayBuffered,
        SendThreadMessage,
        SetTimer,
        TimerKind,
    )
    from ynab_agent.domain.events import (
        AnswerReceived,
        ArchiveWindowReached,
        ClarifyRequested,
        Converged,
        Enriched,
        HoldDeadlineReached,
        HoldResolved,
        InboundReceived,
        LifecycleEvent,
        PatienceExpired,
        SnapshotMaterialized,
        SnapshotUnavailable,
        WriteVerified,
    )
    from ynab_agent.domain.ids import ThreadId, YnabTransactionId
    from ynab_agent.domain.signals import InboundSignal
    from ynab_agent.domain.state_machine import advance
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
        YnabSnapshot,
        born,
    )
    from ynab_agent.policy.converge import classify_verify, target_of
    from ynab_agent.workflow import activities
    from ynab_agent.workflow.types import AnswerOutcome, TransactionParams

_ACTIVITY_TIMEOUT = timedelta(seconds=30)
# History-length ceiling before a resting workflow continues-as-new. High enough
# that ordinary flows never trip it; long-lived (30-45 day) transactions do.
_CONTINUE_AS_NEW_AFTER = 4_000

# Resting states the workflow may sit in for days while waiting on signals or
# timers; continue-as-new fires only from one of these. DISCOVERED counts: a
# transaction born from a signal can wait here for a slow YNAB import (SPEC §3).
_RESTING = (Discovered, AwaitingHuman, Open, Lapsed, HoldAmazon)


def _is_amazon(payee: str) -> bool:
    return "amazon" in payee.lower()


def _hold_for_amazon(snapshot: YnabSnapshot) -> bool:
    """Whether to hold for Amazon item detail: Amazon-ish and no memo yet."""
    return _is_amazon(snapshot.payee) and not snapshot.has_memo


@workflow.defn
class TransactionWorkflow:
    """The durable per-transaction lifecycle."""

    def __init__(self) -> None:
        """Initialize empty state; ``run`` populates it from the params."""
        self._txn: Transaction
        self._ynab_id: str = ""
        self._thread_id: str | None = None
        self._deadlines: dict[TimerKind, datetime.datetime] = {}
        self._inbound: deque[InboundSignal] = deque()
        self._snapshot_ready: YnabSnapshot | None = None
        # Monotonic per-transaction counter: the outbound-send idempotency key
        # so a replay/retry never double-emails (SPEC §3 outbound dedup).
        self._action_seq: int = 0

    # ── Signals (the external world pushes in) ──────────────────────────────
    @workflow.signal
    def submit_inbound(self, signal: InboundSignal) -> None:
        """A reply or matched receipt arrived (W3/W4)."""
        self._inbound.append(signal)

    @workflow.signal
    def notify_snapshot(self, snapshot: YnabSnapshot) -> None:
        """W1 materialized (or backfilled the memo of) the YNAB snapshot."""
        self._snapshot_ready = snapshot

    @workflow.query
    def state(self) -> str:
        """The current lifecycle state (for observability)."""
        return self._txn.state.value

    # ── The durable loop ────────────────────────────────────────────────────
    @workflow.run
    async def run(self, params: TransactionParams) -> None:
        """Drive the lifecycle until the transaction is archived (SPEC §3)."""
        self._ynab_id = params.ynab_id
        self._thread_id = params.thread_id
        self._deadlines = dict(params.resume_deadlines)
        self._inbound.extend(params.resume_inbound)
        self._action_seq = params.resume_action_seq
        self._txn = params.resume_txn or born(
            YnabTransactionId(params.ynab_id), params.thread_id
        )
        self._sync_thread_id()

        while not isinstance(self._txn, Archived):
            await self._step()
            if (
                isinstance(self._txn, _RESTING)
                and not self._inbound  # drain pending signals first (SPEC §0.5)
                and workflow.info().get_current_history_length()
                > _CONTINUE_AS_NEW_AFTER
            ):
                # continue_as_new raises to restart; nothing runs after it.
                workflow.continue_as_new(self._resume_params())

    def _resume_params(self) -> TransactionParams:
        return TransactionParams(
            ynab_id=YnabTransactionId(self._ynab_id),
            thread_id=ThreadId(self._thread_id)
            if self._thread_id is not None
            else None,
            resume_txn=self._txn,
            resume_deadlines=dict(self._deadlines),
            resume_inbound=tuple(self._inbound),
            resume_action_seq=self._action_seq,
        )

    async def _step(self) -> None:
        st = self._txn
        if isinstance(st, Discovered):
            await self._on_discovered()
        elif isinstance(st, Enriching):
            await self._on_enriching(st)
        elif isinstance(st, HoldAmazon):
            await self._on_hold(st)
        elif isinstance(st, AwaitingHuman):
            await self._on_awaiting(st)
        elif isinstance(st, Open):
            await self._on_open()
        elif isinstance(st, Lapsed):
            await self._on_lapsed()
        elif isinstance(st, Revising):
            await self._on_revising(st)
        elif isinstance(st, (AutoApplied, Applied, Archived)):
            # Transient (handled via the commit→verify follow-up) or terminal.
            return

    # ── apply + effect dispatch ─────────────────────────────────────────────
    async def _dispatch(self, event: LifecycleEvent) -> None:
        transition = advance(
            self._txn, event, now=workflow.now(), policy=DEFAULT_POLICY
        )
        self._txn = transition.next
        # Keep the thread-id mirror in step with the state machine, which can
        # adopt a reply's thread on its own (e.g. a reply in DISCOVERED).
        self._sync_thread_id()
        for effect in transition.effects:
            followup = await self._execute(effect)
            if followup is not None:
                await self._dispatch(followup)

    async def _execute(self, effect: Effect) -> LifecycleEvent | None:
        if isinstance(effect, CommitToYnab):
            await workflow.execute_activity(
                activities.commit_to_ynab,
                effect.decision,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
            read = await workflow.execute_activity(
                activities.read_back,
                self._ynab_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
            return WriteVerified(
                outcome=classify_verify(read, target_of(effect.decision))
            )
        if isinstance(effect, OpenThread):
            tid = await workflow.execute_activity(
                activities.open_thread,
                self._ynab_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
            self._set_thread_id(tid)
        elif isinstance(effect, SendThreadMessage):
            self._action_seq += 1
            await workflow.execute_activity(
                activities.send_thread_message,
                args=[self._thread_id, effect.purpose, self._action_seq],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        elif isinstance(effect, FeedRuleLearning):
            await workflow.execute_activity(
                activities.feed_rule_learning,
                args=[effect.event, effect.decision, effect.prior],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        elif isinstance(effect, CloseThread):
            if self._thread_id is not None:
                await workflow.execute_activity(
                    activities.close_thread,
                    self._thread_id,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )
        elif isinstance(effect, SetTimer):
            self._deadlines[effect.timer] = effect.deadline
        elif isinstance(effect, CancelTimer):
            self._deadlines.pop(effect.timer, None)
        elif isinstance(effect, ReplayBuffered):
            self._inbound.extendleft(reversed(effect.signals))
        return None

    def _set_thread_id(self, tid: str) -> None:
        self._thread_id = tid
        st = self._txn
        if not isinstance(st, Discovered):
            new_core = st.core.model_copy(update={"thread_id": ThreadId(tid)})
            self._txn = st.model_copy(update={"core": new_core})

    def _sync_thread_id(self) -> None:
        """Mirror the current transaction's thread id (SM may adopt it)."""
        st = self._txn
        tid = st.thread_id if isinstance(st, Discovered) else st.core.thread_id
        if tid is not None:
            self._thread_id = str(tid)

    # ── per-state steps ─────────────────────────────────────────────────────
    async def _on_discovered(self) -> None:
        snapshot = await workflow.execute_activity(
            activities.fetch_snapshot,
            self._ynab_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        if snapshot is not None:
            await self._dispatch(
                SnapshotMaterialized(
                    snapshot=snapshot,
                    hold_for_amazon=_hold_for_amazon(snapshot),
                )
            )
            return
        # Signal beat the poll: wait for materialization or buffer an inbound.
        await self._dispatch(SnapshotUnavailable())
        await workflow.wait_condition(
            lambda: self._snapshot_ready is not None or len(self._inbound) > 0
        )
        if self._snapshot_ready is not None:
            snap = self._snapshot_ready
            self._snapshot_ready = None
            await self._dispatch(
                SnapshotMaterialized(
                    snapshot=snap, hold_for_amazon=_hold_for_amazon(snap)
                )
            )
        else:
            await self._dispatch(
                InboundReceived(signal=self._inbound.popleft())
            )

    async def _on_enriching(self, st: Enriching) -> None:
        outcome = await workflow.execute_activity(
            activities.enrich,
            st.core.snapshot,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        await self._dispatch(Enriched(outcome=outcome))

    async def _on_hold(self, st: HoldAmazon) -> None:
        ready = await self._wait_until(
            st.amazon_deadline,
            lambda: self._snapshot_ready is not None or len(self._inbound) > 0,
        )
        if not ready:
            await self._dispatch(HoldDeadlineReached())
        elif self._snapshot_ready is not None:
            snap = self._snapshot_ready
            self._snapshot_ready = None
            await self._dispatch(HoldResolved(snapshot=snap))
        else:
            await self._dispatch(
                InboundReceived(signal=self._inbound.popleft())
            )

    async def _on_awaiting(self, st: AwaitingHuman) -> None:
        got_inbound = await self._wait_until(
            st.patience_deadline, self._has_inbound
        )
        if not got_inbound:
            before = type(self._txn)
            await self._dispatch(PatienceExpired())
            if type(self._txn) is not before:
                return  # lapsed (or otherwise transitioned)
            # Flagged (verify-failure) entry: PatienceExpired is ignored and
            # does not lapse (SPEC §3). Drop the passed timer and wait for an
            # inbound instead of re-spinning the expired deadline.
            self._deadlines.pop(TimerKind.PATIENCE, None)
            await workflow.wait_condition(self._has_inbound)
        await self._interpret_inbound(st.core.snapshot)

    async def _interpret_inbound(self, snapshot: YnabSnapshot) -> None:
        signal = self._inbound.popleft()
        interpretation = await workflow.execute_activity(
            activities.interpret_inbound,
            args=[signal, snapshot],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        if isinstance(interpretation, AnswerOutcome):
            await self._dispatch(
                AnswerReceived(decision=interpretation.decision)
            )
        else:
            await self._dispatch(
                ClarifyRequested(question=interpretation.question)
            )

    async def _on_open(self) -> None:
        await self._wait_then_revise_or_archive()

    async def _on_lapsed(self) -> None:
        await self._wait_then_revise_or_archive()

    def _has_inbound(self) -> bool:
        return len(self._inbound) > 0

    async def _wait_then_revise_or_archive(self) -> None:
        deadline = self._deadlines.get(TimerKind.ARCHIVE)
        got_inbound = (
            await self._wait_until(deadline, self._has_inbound)
            if deadline is not None
            else await self._wait_forever(self._has_inbound)
        )
        if got_inbound:
            await self._dispatch(
                InboundReceived(signal=self._inbound.popleft())
            )
            return
        # The archive window elapsed. Attempt to archive; if it is blocked (not
        # reconciled / not categorized) the state is unchanged, so drop the
        # now-stale timer and wait for an inbound rather than busy-looping on
        # the already-passed deadline.
        before = type(self._txn)
        await self._dispatch(ArchiveWindowReached())
        if type(self._txn) is before:
            self._deadlines.pop(TimerKind.ARCHIVE, None)
            await workflow.wait_condition(self._has_inbound)
            await self._dispatch(
                InboundReceived(signal=self._inbound.popleft())
            )

    async def _wait_forever(self, predicate: Callable[[], bool]) -> bool:
        await workflow.wait_condition(predicate)
        return True

    async def _on_revising(self, st: Revising) -> None:
        outcome = await workflow.execute_activity(
            activities.converge,
            args=[st.core.snapshot, st.instruction],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        await self._dispatch(Converged(outcome=outcome))

    async def _wait_until(
        self,
        deadline: datetime.datetime,
        predicate: Callable[[], bool],
    ) -> bool:
        """Wait for ``predicate`` until an absolute deadline.

        Returns ``True`` if the predicate fired first, ``False`` on timeout.
        """
        timeout = deadline - workflow.now()
        if timeout < timedelta(0):
            timeout = timedelta(0)
        try:
            await workflow.wait_condition(predicate, timeout=timeout)
        except TimeoutError:
            return False
        return True
