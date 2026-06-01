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
