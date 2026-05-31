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
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.domain.allocations import ResolvedCategory
from ynab_agent.domain.enums import ClearedState, DecidedBy
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
    ThreadId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision, Proposal
from ynab_agent.domain.signals import ReplySignal
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import target_of
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
) -> list[Callable[..., object]]:
    """Build mock activity implementations for one scenario."""
    committed: dict[str, object] = {}

    @activity.defn(name="fetch_snapshot")
    async def fetch_snapshot(ynab_id: str) -> YnabSnapshot | None:
        return snapshot

    @activity.defn(name="enrich")
    async def enrich(snap: YnabSnapshot) -> EnrichmentOutcome:
        return enrich_outcome

    @activity.defn(name="commit_to_ynab")
    async def commit_to_ynab(decision: Decision) -> None:
        committed["target"] = target_of(decision)

    @activity.defn(name="read_back")
    async def read_back(ynab_id: str) -> object:
        return committed.get("target")

    @activity.defn(name="open_thread")
    async def open_thread(ynab_id: str) -> str:
        return "thread-1"

    @activity.defn(name="send_thread_message")
    async def send_thread_message(
        thread_id: str | None, purpose: object
    ) -> None:
        return None

    @activity.defn(name="interpret_reply")
    async def interpret_reply(signal: object, snap: object) -> ReplyOutcome:
        assert interpret is not None
        return interpret

    @activity.defn(name="converge")
    async def converge(snap: object, instruction: object) -> ConvergeOutcome:
        assert converge_outcome is not None
        return converge_outcome

    @activity.defn(name="feed_rule_learning")
    async def feed_rule_learning(
        event: object, decision: object, prior: object
    ) -> None:
        return None

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
        interpret_reply,
        converge,
        feed_rule_learning,
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
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AutoApply(decision=_decision(DecidedBy.AGENT)),
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
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


async def test_ask_then_answer_reaches_open_and_archives() -> None:
    acts = _activities(
        snapshot=_snapshot(reconciled=True),
        enrich_outcome=AskHuman(proposal=_proposal()),
        interpret=AnswerOutcome(decision=_decision(DecidedBy.HUMAN)),
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
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


async def test_patience_timeout_lapses() -> None:
    acts = _activities(
        snapshot=_snapshot(reconciled=False),
        enrich_outcome=AskHuman(proposal=_proposal()),
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
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
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
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
