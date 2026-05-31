"""End-to-end tests for the W1 poll workflow on the time-skipping server."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from temporalio import activity
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
    *, page: DeltaPage, addressed: list[str], saved: list[int]
) -> list[Callable[..., object]]:
    @activity.defn(name="fetch_delta")
    async def fetch_delta(budget_id: str, cursor: int | None) -> DeltaPage:
        return page

    @activity.defn(name="address_transaction")
    async def address_transaction(action: AddressTxn) -> None:
        addressed.append(action.snapshot.ynab_id)

    @activity.defn(name="save_cursor")
    async def save_cursor(budget_id: str, server_knowledge: int) -> None:
        saved.append(server_knowledge)

    return [fetch_delta, address_transaction, save_cursor]


async def _run(
    *, wf_id: str, page: DeltaPage, params: PollParams
) -> tuple[PollResult, list[str]]:
    addressed: list[str] = []
    saved: list[int] = []
    acts = _poll_activities(page=page, addressed=addressed, saved=saved)
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
    assert saved == [page.server_knowledge]  # cursor always advances
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
    # cursor=None → cold start: nothing addressed, but the cursor is saved.
    result, addressed = await _run(
        wf_id="poll-cold",
        page=page,
        params=PollParams(scope=_scope(), cursor=None),
    )
    assert result.addressed == 0
    assert addressed == []
    assert result.new_cursor == 7
