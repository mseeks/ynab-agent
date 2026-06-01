"""End-to-end tests for the W1 poll workflow on the time-skipping server.

The one-shot path (``continuous=False``) covers the per-tick logic — cold-start
capture and in-scope addressing. A separate test drives the durable loop
(``continuous=True``) and proves the delta cursor is carried across
continue-as-new in workflow state (no external cursor store, SPEC §0.5).
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
from ynab_agent.workflow.poll_types import DeltaPage, PollParams, PollResult
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
    *, page: DeltaPage, addressed: list[str]
) -> list[Callable[..., object]]:
    @activity.defn(name="fetch_delta")
    async def fetch_delta(scope: IngestScope, cursor: int | None) -> DeltaPage:
        return page

    @activity.defn(name="address_transaction")
    async def address_transaction(action: AddressTxn) -> None:
        addressed.append(action.snapshot.ynab_id)

    return [fetch_delta, address_transaction]


async def _run(
    *, wf_id: str, page: DeltaPage, params: PollParams
) -> tuple[PollResult, list[str]]:
    addressed: list[str] = []
    acts = _poll_activities(page=page, addressed=addressed)
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


async def test_poll_addresses_in_scope_transactions() -> None:
    page = DeltaPage(
        snapshots=(
            _snapshot("t1", account="a1"),
            _snapshot("t2", account="a1"),
        ),
        server_knowledge=42,
    )
    result, addressed = await _run(
        wf_id="poll-inscope",
        page=page,
        params=PollParams(scope=_scope(), cursor=5),
    )
    assert result.addressed == 2
    assert set(addressed) == {"t1", "t2"}
    assert result.new_cursor == 42


async def test_cold_start_captures_cursor_without_acting() -> None:
    page = DeltaPage(snapshots=(_snapshot("t1"),), server_knowledge=7)
    # cursor=None → cold start: nothing addressed, but the cursor advances.
    result, addressed = await _run(
        wf_id="poll-cold",
        page=page,
        params=PollParams(scope=_scope(), cursor=None),
    )
    assert result.addressed == 0
    assert addressed == []
    assert result.new_cursor == 7


async def test_continuous_loop_carries_cursor_across_continue_as_new() -> None:
    # The durable loop has no external cursor store: each tick must continue-as
    # -new with the prior tick's server_knowledge. Capture the cursor each tick
    # and break the otherwise-infinite loop on the second by raising.
    cursors: list[int | None] = []

    @activity.defn(name="fetch_delta")
    async def fetch_delta(scope: IngestScope, cursor: int | None) -> DeltaPage:
        cursors.append(cursor)
        if len(cursors) >= 2:
            raise ApplicationError("stop the loop", non_retryable=True)
        return DeltaPage(snapshots=(), server_knowledge=99)

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
            activities=[fetch_delta, address_transaction],
        ),
    ):
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                PollWorkflow.run,
                PollParams(scope=_scope(), cursor=None, continuous=True),
                id="poll-loop",
                task_queue=TASK_QUEUE,
            )
    # Tick 1 ran cold (None); tick 2 inherited the advanced cursor from state.
    assert cursors == [None, 99]
