"""End-to-end tests for the W2 workflow on Temporal's time-skipping server.

These run the real durable workflow (signals, absolute-deadline timers, the
commit→verify follow-up, continue-as-new guard) with mock activity
implementations standing in for the stubbed I/O ports.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.domain.allocations import ProposedCategory, ResolvedCategory
from ynab_agent.domain.effects import FeedRuleLearning, RuleLearningKind
from ynab_agent.domain.enums import ClearedState, DecidedBy, TrustState
from ynab_agent.domain.events import (
    AskHuman,
    AutoApply,
    ConvergeOutcome,
    EnrichmentOutcome,
    Reapplied,
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
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.learn.handler import plan_rule_update
from ynab_agent.policy.converge import TargetState, target_of
from ynab_agent.workflow.runtime import DATA_CONVERTER
from ynab_agent.workflow.txn_workflow import TransactionWorkflow
from ynab_agent.workflow.types import (
    AnswerOutcome,
    ReplyOutcome,
    TransactionParams,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from temporalio.client import WorkflowHandle

TASK_QUEUE = "ynab-test"
_EPOCH = datetime.datetime(2026, 5, 28, tzinfo=datetime.UTC)


async def _start_env() -> WorkflowEnvironment:
    """A time-skipping env with the TxnThreadId search attribute registered.

    The workflow upserts ``TxnThreadId`` on ``open_thread``; the time-skipping
    test server *hangs* the workflow task on an unregistered search attribute,
    register it before any run (the real cluster registers it via
    ``manage/search-attributes.yaml``).
    """
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    )
    await env.client.operator_service.add_search_attributes(
        AddSearchAttributesRequest(
            namespace="default",
            search_attributes={
                "TxnThreadId": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD
            },
        )
    )
    return env


def _snapshot(*, reconciled: bool = True) -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId("t1"),
        account=AccountId("a1"),
        payee="Blue Bottle",
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, 28),
        category_id=CategoryId("dining"),
        cleared=ClearedState.RECONCILED if reconciled else ClearedState.CLEARED,
    )


def _decision(by: DecidedBy) -> Decision:
    return Decision(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        approved=True,
        decided_by=by,
        decided_at=_EPOCH,
    )


def _proposal() -> Proposal:
    from ynab_agent.domain.allocations import ProposedCategory
    from ynab_agent.domain.enums import Confidence

    return Proposal(
        allocation=ProposedCategory(category=CategoryId("dining")),
        confidence=Confidence.HIGH,
        rationale="known merchant",
    )


def _reply() -> ReplySignal:
    return ReplySignal(
        thread_id=ThreadId("thread-1"),
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        text="ok",
    )


def _activities(
    *,
    snapshot: YnabSnapshot,
    enrich_outcome: EnrichmentOutcome,
    interpret: ReplyOutcome | None = None,
    converge_outcome: ConvergeOutcome | None = None,
    read_back_seq: list[object] | None = None,
    learning_sink: list[FeedRuleLearning] | None = None,
    auto_action_sink: list[str] | None = None,
    enrich_snapshot_sink: list[YnabSnapshot] | None = None,
) -> list[Callable[..., object]]:
    """Build mock activity implementations for one scenario."""
    committed: dict[str, object] = {}
    read_seq = list(read_back_seq or [])

    @activity.defn(name="fetch_snapshot")
    async def fetch_snapshot(ynab_id: str) -> YnabSnapshot | None:
        return snapshot

    @activity.defn(name="enrich")
    async def enrich(snap: YnabSnapshot) -> EnrichmentOutcome:
        if enrich_snapshot_sink is not None:
            enrich_snapshot_sink.append(snap)
        return enrich_outcome

    @activity.defn(name="commit_to_ynab")
    async def commit_to_ynab(ynab_id: str, decision: Decision) -> None:
        committed["target"] = target_of(decision)

    @activity.defn(name="read_back")
    async def read_back(ynab_id: str) -> object:
        if read_seq:
            return read_seq.pop(0)
        return committed.get("target")

    @activity.defn(name="open_thread")
    async def open_thread(ynab_id: str, proposal: object) -> str:
        return "thread-1"

    @activity.defn(name="send_thread_message")
    async def send_thread_message(
        ynab_id: str,
        thread_id: str | None,
        purpose: object,
        action_seq: int,
        proposal: object,
    ) -> None:
        return None

    @activity.defn(name="interpret_inbound")
    async def interpret_inbound(
        signal: object, snap: object, proposal: object
    ) -> ReplyOutcome:
        assert interpret is not None
        return interpret

    @activity.defn(name="converge")
    async def converge(
        snap: object, instruction: object, prior: object
    ) -> ConvergeOutcome:
        assert converge_outcome is not None
        return converge_outcome

    @activity.defn(name="feed_rule_learning")
    async def feed_rule_learning(feed: FeedRuleLearning) -> None:
        if learning_sink is not None:
            learning_sink.append(feed)

    @activity.defn(name="record_auto_action")
    async def record_auto_action(ynab_id: str) -> None:
        if auto_action_sink is not None:
            auto_action_sink.append(ynab_id)

    @activity.defn(name="close_thread")
    async def close_thread(thread_id: str) -> None:
        return None

    return [
        fetch_snapshot,
        enrich,
        commit_to_ynab,
        read_back,
        open_thread,
        send_thread_message,
        interpret_inbound,
        converge,
        feed_rule_learning,
        record_auto_action,
        close_thread,
    ]


async def _wait_for_state(
    handle: WorkflowHandle[TransactionWorkflow, None],
    target: str,
    tries: int = 60,
) -> None:
    for _ in range(tries):
        if await handle.query(TransactionWorkflow.state) == target:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached state {target!r}")


async def test_auto_apply_flows_to_open_then_archives() -> None:
    auto_actions: list[str] = []
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AutoApply(decision=_decision(DecidedBy.AGENT)),
        auto_action_sink=auto_actions,
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-auto",
            task_queue=TASK_QUEUE,
        )
        # Reconciled → the archive timer (time-skipped) drives it to done.
        await handle.result()
    # The auto-apply recorded itself in the circuit-breaker ledger (SPEC §0.6).
    assert auto_actions == ["t1"]


async def test_hold_amazon_resolves_on_memo_backfill_signal() -> None:
    # A blank Amazon txn holds for item detail; a notify_snapshot carrying the
    # backfilled memo (what W1 delivers, SPEC §2/§3) resolves the hold, and
    # enrichment runs on the snapshot *with* the memo — not the held blank one
    # the ~36h deadline fallback would use.
    held = YnabSnapshot(
        ynab_id=YnabTransactionId("t1"),
        account=AccountId("a1"),
        payee="Amazon",
        amount=Money.from_currency("-31.40"),
        txn_date=datetime.date(2026, 5, 28),
        cleared=ClearedState.RECONCILED,
    )
    backfilled = held.model_copy(update={"memo": "AmazonBasics HDMI cable"})
    enriched: list[YnabSnapshot] = []
    acts = _activities(
        snapshot=held,
        enrich_outcome=AskHuman(proposal=_proposal()),
        enrich_snapshot_sink=enriched,
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        # signal-with-start delivers the backfill snapshot as W1 would, so the
        # hold resolves to its memo rather than waiting out the deadline.
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-hold",
            task_queue=TASK_QUEUE,
            start_signal="notify_snapshot",
            start_signal_args=[backfilled],
        )
        await _wait_for_state(handle, "awaiting_human")
    assert enriched
    assert enriched[-1].memo == "AmazonBasics HDMI cable"


async def test_ask_then_answer_reaches_open_and_archives() -> None:
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AskHuman(proposal=_proposal()),
        interpret=AnswerOutcome(decision=_decision(DecidedBy.HUMAN)),
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        # signal-with-start: the reply is buffered before AWAITING_HUMAN.
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-answer",
            task_queue=TASK_QUEUE,
            start_signal="submit_inbound",
            start_signal_args=[_reply()],
        )
        await handle.result()


async def test_human_confirm_feeds_rule_learning() -> None:
    # A human answer drives APPLIED → OPEN, which emits the W5 effect carrying
    # the payee + the human decision; plan_rule_update then learns the rule.
    sink: list[FeedRuleLearning] = []
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AskHuman(proposal=_proposal()),
        interpret=AnswerOutcome(decision=_decision(DecidedBy.HUMAN)),
        learning_sink=sink,
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-learn",
            task_queue=TASK_QUEUE,
            start_signal="submit_inbound",
            start_signal_args=[_reply()],
        )
        await handle.result()

    assert len(sink) == 1
    feed = sink[0]
    assert feed.event is RuleLearningKind.CONFIRM
    assert feed.payee == "Blue Bottle"

    # The captured effect, fed to the handler, learns a confirmed dining rule.
    outcome = plan_rule_update((), feed, now=_EPOCH, next_id=RuleId("r-new"))
    assert outcome is not None
    rule = outcome.rules[0]
    assert rule.trust is TrustState.CONFIRMED
    assert isinstance(rule.action.allocation, ProposedCategory)
    assert rule.action.allocation.category == "dining"


async def test_patience_timeout_lapses() -> None:
    acts = _activities(
        snapshot=_snapshot(reconciled=False),
        enrich_outcome=AskHuman(proposal=_proposal()),
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-lapse",
            task_queue=TASK_QUEUE,
        )
        # Reach the resting ask, then skip past the ~7d patience window.
        await _wait_for_state(handle, "awaiting_human")
        await env.sleep(datetime.timedelta(days=8))
        await _wait_for_state(handle, "lapsed")


async def test_open_inbound_revises_then_archives() -> None:
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AutoApply(decision=_decision(DecidedBy.AGENT)),
        converge_outcome=Reapplied(decision=_decision(DecidedBy.HUMAN)),
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        # The buffered reply is consumed in OPEN → REVISING → converge.
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-revise",
            task_queue=TASK_QUEUE,
            start_signal="submit_inbound",
            start_signal_args=[_reply()],
        )
        await handle.result()


async def test_open_manual_edit_at_archive_demotes_and_archives() -> None:
    # AUTO applies "dining"; the verify read matches (→ OPEN). At the archive
    # boundary a re-read shows the owner recategorized to "gifts" in YNAB — a
    # silent override that must feed a CORRECT demotion before closing (§14.2).
    sink: list[FeedRuleLearning] = []
    auto = _decision(DecidedBy.AGENT).model_copy(
        update={"rule_id": RuleId("r1")}
    )
    matches = TargetState(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        approved=True,
    )
    overridden = TargetState(
        allocation=ResolvedCategory(category=CategoryId("gifts")),
        approved=True,
    )
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AutoApply(decision=auto),
        read_back_seq=[matches, overridden],
        learning_sink=sink,
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-override",
            task_queue=TASK_QUEUE,
        )
        await handle.result()

    assert len(sink) == 1
    feed = sink[0]
    assert feed.event is RuleLearningKind.CORRECT
    # The prior (the agent's auto decision) names the rule to demote.
    assert feed.prior is not None
    assert feed.prior.rule_id == "r1"
    assert feed.decision is not None
    assert isinstance(feed.decision.allocation, ResolvedCategory)
    assert feed.decision.allocation.category == "gifts"


async def test_diverged_verify_flags_awaiting_and_does_not_livelock() -> None:
    # First read-back diverges (forcing a flagged AWAITING_HUMAN); the second
    # (after the human reply re-commits) matches.
    divergent = TargetState(
        allocation=ResolvedCategory(category=CategoryId("gifts")),
        approved=True,
    )
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AutoApply(decision=_decision(DecidedBy.AGENT)),
        interpret=AnswerOutcome(decision=_decision(DecidedBy.HUMAN)),
        read_back_seq=[divergent],
    )
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TransactionWorkflow],
            activities=acts,
        ),
    ):
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-diverge",
            task_queue=TASK_QUEUE,
        )
        # The diverged verify parks it in a flagged AWAITING_HUMAN.
        await _wait_for_state(handle, "awaiting_human")
        # Past the patience window it must NOT lapse and must NOT livelock.
        await env.sleep(datetime.timedelta(days=8))
        assert await handle.query(TransactionWorkflow.state) == "awaiting_human"
        # A reply still resolves it, all the way to archived.
        await handle.signal(TransactionWorkflow.submit_inbound, _reply())
        await handle.result()
