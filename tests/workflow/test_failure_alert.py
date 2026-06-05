"""W2 pages the owner once when an activity fails terminally (SPEC §13).

The whole alert path, end to end on the time-skipping server: a non-retryable
``enrich`` failure trips the workflow's terminal-failure hook, which runs the
*real* ``alert_failure`` activity — it consults the durable ledger, pushes
through a fake ntfy backend, and records the alert — then re-raises so the
transaction still fails. Proves the hook fires, the push carries the failing
activity + payee, and the workflow surfaces the failure rather than swallowing
it.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import ynab_agent.notify.client as notify_client
import ynab_agent.workflow.temporal_client as temporal_client
from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.enums import ClearedState
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    MessageId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.notify.client import Notification, NotifyClient
from ynab_agent.workflow import alert_activities
from ynab_agent.workflow.alert_ledger_workflow import AlertLedgerWorkflow
from ynab_agent.workflow.dispatch_types import DispatchParams
from ynab_agent.workflow.dispatch_workflow import DispatchWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER
from ynab_agent.workflow.txn_workflow import TransactionWorkflow
from ynab_agent.workflow.types import TransactionParams

if TYPE_CHECKING:
    from collections.abc import Callable

_TASK_QUEUE = "ynab-failure-alert-test"


class _RecordingBackend:
    """A fake ntfy backend that captures every notification published."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def publish(self, notification: Notification) -> None:
        self.sent.append(notification)


def _snapshot() -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId("t1"),
        account=AccountId("a1"),
        payee="Blue Bottle",
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, 28),
        category_id=CategoryId("dining"),
        cleared=ClearedState.CLEARED,
    )


def _failing_enrich_activities() -> list[Callable[..., object]]:
    @activity.defn(name="fetch_snapshot")
    async def fetch_snapshot(ynab_id: str) -> YnabSnapshot:
        return _snapshot()

    @activity.defn(name="enrich")
    async def enrich(snapshot: YnabSnapshot) -> object:
        # ValueError is on the non-retryable list → terminal on attempt 1.
        msg = "boom in the model layer"
        raise ValueError(msg)

    # The real alert_failure runs here (not a mock) — that is the point.
    return [fetch_snapshot, enrich, alert_activities.alert_failure]


async def test_terminal_enrich_failure_pages_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _RecordingBackend()
    monkeypatch.setattr(notify_client, "_CACHED", NotifyClient(backend))
    # alert_failure reaches the ledger through these process globals.
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", _TASK_QUEUE)

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[TransactionWorkflow, AlertLedgerWorkflow],
            activities=_failing_enrich_activities(),
        ),
    ):
        monkeypatch.setattr(temporal_client, "_CLIENT", env.client)
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-fail-1",
            task_queue=_TASK_QUEUE,
        )

        # The transaction still fails terminally (the alert never swallows it).
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    # Exactly one push, naming the failing activity and the payee.
    assert len(backend.sent) == 1
    pushed = backend.sent[0]
    assert "enrich" in pushed.title
    assert "Blue Bottle" in pushed.body
    assert "ValueError" in pushed.body


def _failing_dispatch_activities() -> list[Callable[..., object]]:
    @activity.defn(name="resolve_thread")
    async def resolve_thread(thread: str | None) -> str | None:
        msg = "boom resolving the thread"
        raise ValueError(msg)

    return [resolve_thread, alert_activities.alert_failure]


async def test_dispatch_terminal_failure_pages_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _RecordingBackend()
    monkeypatch.setattr(notify_client, "_CACHED", NotifyClient(backend))
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", _TASK_QUEUE)

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[DispatchWorkflow, AlertLedgerWorkflow],
            activities=_failing_dispatch_activities(),
        ),
    ):
        monkeypatch.setattr(temporal_client, "_CLIENT", env.client)
        message = InboundMessage(
            message_id=MessageId("m1"),
            from_address="matthew@example.com",
            subject="[YNAB] $4.50",
            body="ok",
        )
        handle = await env.client.start_workflow(
            DispatchWorkflow.run,
            DispatchParams(
                message=message, allowlist=frozenset({"matthew@example.com"})
            ),
            id="dispatch-fail-1",
            task_queue=_TASK_QUEUE,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    # One push, naming the failing activity and the sender.
    assert len(backend.sent) == 1
    pushed = backend.sent[0]
    assert "resolve_thread" in pushed.title
    assert "matthew@example.com" in pushed.body


def _retryable_failing_activities() -> list[Callable[..., object]]:
    @activity.defn(name="fetch_snapshot")
    async def fetch_snapshot(ynab_id: str) -> YnabSnapshot:
        return _snapshot()

    @activity.defn(name="enrich")
    async def enrich(snapshot: YnabSnapshot) -> object:
        # RuntimeError is NOT on the denylist -> retryable. Without a bounded
        # maximum_attempts this would retry forever and the test would hang;
        # the bound makes it terminate after the cap and page.
        msg = "transient model wobble"
        raise RuntimeError(msg)

    return [fetch_snapshot, enrich, alert_activities.alert_failure]


async def test_retryable_failure_exhausts_attempts_and_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _RecordingBackend()
    monkeypatch.setattr(notify_client, "_CACHED", NotifyClient(backend))
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", _TASK_QUEUE)

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[TransactionWorkflow, AlertLedgerWorkflow],
            activities=_retryable_failing_activities(),
        ),
    ):
        monkeypatch.setattr(temporal_client, "_CLIENT", env.client)
        handle = await env.client.start_workflow(
            TransactionWorkflow.run,
            TransactionParams(ynab_id=YnabTransactionId("t1")),
            id="txn-retry-exhaust",
            task_queue=_TASK_QUEUE,
        )
        # Terminates (does not retry forever) and surfaces the failure.
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    assert len(backend.sent) == 1
    assert "enrich" in backend.sent[0].title
