"""End-to-end tests for the W1 poll workflow on the time-skipping server.

The one-shot path (``continuous=False``) covers the per-tick logic — addressing
the in-scope unapproved transactions. A separate test drives the durable loop
(``continuous=True``) and proves it keeps ticking via continue-as-new (no cursor
to carry — the outstanding set is re-read from YNAB each tick, SPEC §0.5).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.domain.ids import AccountId, YnabTransactionId
from ynab_agent.domain.money import Money
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.ingest.plan import AddressTxn
from ynab_agent.ingest.scope import IngestScope
from ynab_agent.workflow.alert_types import FailureAlert
from ynab_agent.workflow.poll_types import PollParams, PollResult
from ynab_agent.workflow.poll_workflow import PollWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

TASK_QUEUE = "ynab-poll-test"


def _snapshot(
    ynab_id: str, *, account: str = "a1", day: int = 15
) -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId(ynab_id),
        account=AccountId(account),
        payee="Blue Bottle",
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, day),
    )


def _scope() -> IngestScope:
    return IngestScope(budget_id="b1", install_date=datetime.date(2026, 5, 1))


def _poll_activities(
    *, unapproved: tuple[YnabSnapshot, ...], addressed: list[str]
) -> list[Callable[..., object]]:
    @activity.defn(name="fetch_unapproved")
    async def fetch_unapproved() -> tuple[YnabSnapshot, ...]:
        return unapproved

    @activity.defn(name="address_transaction")
    async def address_transaction(action: AddressTxn) -> None:
        addressed.append(action.snapshot.ynab_id)

    return [fetch_unapproved, address_transaction]


async def _run(
    *, wf_id: str, unapproved: tuple[YnabSnapshot, ...], params: PollParams
) -> tuple[PollResult, list[str]]:
    addressed: list[str] = []
    acts = _poll_activities(unapproved=unapproved, addressed=addressed)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[PollWorkflow],
            activities=acts,
        ),
    ):
        result = await env.client.execute_workflow(
            PollWorkflow.run, params, id=wf_id, task_queue=TASK_QUEUE
        )
    return result, addressed


async def test_poll_addresses_in_scope_unapproved() -> None:
    unapproved = (
        _snapshot("t1", account="a1"),
        _snapshot("t2", account="a1"),
        # out of scope: dated before install — never addressed.
        _snapshot("t3", account="a1", day=15).model_copy(
            update={"txn_date": datetime.date(2026, 4, 1)}
        ),
    )
    result, addressed = await _run(
        wf_id="poll-inscope",
        unapproved=unapproved,
        params=PollParams(scope=_scope()),
    )
    assert result.addressed == 2
    assert set(addressed) == {"t1", "t2"}


async def test_poll_addresses_nothing_when_set_empty() -> None:
    result, addressed = await _run(
        wf_id="poll-empty",
        unapproved=(),
        params=PollParams(scope=_scope()),
    )
    assert result.addressed == 0
    assert addressed == []


async def test_continuous_loop_keeps_ticking_via_continue_as_new() -> None:
    # The durable loop re-reads the unapproved set each tick. Count ticks and
    # break the otherwise-infinite loop on the second by raising.
    ticks: list[int] = []

    @activity.defn(name="fetch_unapproved")
    async def fetch_unapproved() -> tuple[YnabSnapshot, ...]:
        ticks.append(1)
        if len(ticks) >= 2:
            raise ApplicationError("stop the loop", non_retryable=True)
        return ()

    @activity.defn(name="address_transaction")
    async def address_transaction(action: AddressTxn) -> None:
        return None

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[PollWorkflow],
            activities=[fetch_unapproved, address_transaction],
        ),
    ):
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                PollWorkflow.run,
                PollParams(scope=_scope(), continuous=True),
                id="poll-loop",
                task_queue=TASK_QUEUE,
            )
    # A second tick ran only because the first continued-as-new.
    assert len(ticks) == 2


async def test_address_transaction_signals_amazon_backfill_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The W2 already exists; an Amazon memo has backfilled, so W1 signals
    # notify_snapshot to wake a HOLD_AMAZON run early (SPEC §2, §3). Without the
    # backfill flag, an already-running W2 is left untouched.
    from temporalio.exceptions import WorkflowAlreadyStartedError

    import ynab_agent.workflow.temporal_client as temporal_client
    from ynab_agent.workflow import poll_activities

    class _Handle:
        def __init__(self) -> None:
            self.signals: list[tuple[str, object]] = []

        async def signal(self, name: str, arg: object) -> None:
            self.signals.append((name, arg))

    class _Client:
        def __init__(self) -> None:
            self.handle = _Handle()

        async def start_workflow(self, *args: object, **kwargs: object) -> None:
            raise WorkflowAlreadyStartedError(
                workflow_id=str(kwargs.get("id")),
                workflow_type="TransactionWorkflow",
            )

        def get_workflow_handle(self, workflow_id: str) -> _Handle:
            return self.handle

    snap = _snapshot("t1").model_copy(
        update={"payee": "Amazon", "memo": "HDMI cable"}
    )

    backfill = _Client()

    async def _backfill_client() -> _Client:
        return backfill

    monkeypatch.setattr(temporal_client, "client", _backfill_client)
    await poll_activities.address_transaction(
        AddressTxn(snapshot=snap, notify_existing=True)
    )
    assert backfill.handle.signals == [("notify_snapshot", snap)]

    plain = _Client()

    async def _plain_client() -> _Client:
        return plain

    monkeypatch.setattr(temporal_client, "client", _plain_client)
    await poll_activities.address_transaction(
        AddressTxn(snapshot=snap, notify_existing=False)
    )
    assert plain.handle.signals == []


async def test_continuous_loop_survives_a_failed_tick_and_pages() -> None:
    # A tick whose activity fails terminally must NOT kill the loop (the §13
    # silent stop): it pages (deduped, best-effort) and the next tick runs.
    # Every tick here fails; the loop survives tick 1 (its page succeeds) and
    # is ended by the test on tick 2, whose page raises non-retryably.
    ticks: list[int] = []
    alerts: list[str] = []

    @activity.defn(name="fetch_unapproved")
    async def fetch_unapproved() -> tuple[YnabSnapshot, ...]:
        ticks.append(1)
        raise ApplicationError("YNAB unreachable", non_retryable=True)

    @activity.defn(name="address_transaction")
    async def address_transaction(action: AddressTxn) -> None:
        return None

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: FailureAlert) -> None:
        alerts.append(alert.key)
        if len(alerts) >= 2:
            raise ApplicationError("end the test", non_retryable=True)

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[PollWorkflow],
            activities=[fetch_unapproved, address_transaction, alert_failure],
        ),
    ):
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                PollWorkflow.run,
                PollParams(scope=_scope(), continuous=True),
                id="poll-survive",
                task_queue=TASK_QUEUE,
            )
    # Tick 1 failed and PAGED, then the loop slept and ran tick 2 — it
    # outlived the outage instead of dying with it.
    assert len(ticks) == 2
    assert alerts.count("w1-poll-tick") == 2


async def test_one_shot_tick_failure_still_raises() -> None:
    # A manual/one-shot run must surface the real error, not swallow it.
    @activity.defn(name="fetch_unapproved")
    async def fetch_unapproved() -> tuple[YnabSnapshot, ...]:
        raise ApplicationError("boom", non_retryable=True)

    @activity.defn(name="address_transaction")
    async def address_transaction(action: AddressTxn) -> None:
        return None

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: object) -> None:
        raise AssertionError("a one-shot failure must not page")

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[PollWorkflow],
            activities=[fetch_unapproved, address_transaction, alert_failure],
        ),
    ):
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                PollWorkflow.run,
                PollParams(scope=_scope()),
                id="poll-oneshot-fail",
                task_queue=TASK_QUEUE,
            )
