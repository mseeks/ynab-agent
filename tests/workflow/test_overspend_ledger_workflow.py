"""The durable overspend-alert dedup ledger on the time-skipping server.

Verifies the wiring around the pure folds (tested in ``tests/budget``): the
``record`` signal folds an alert in, and the ``prior`` query reads it back —
hydrated as a real ``PriorAlert`` over the pydantic converter — resetting across
a period boundary and staying per-category.
"""

from __future__ import annotations

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.budget.overspend import OverspendVerdict, PriorAlert
from ynab_agent.domain.money import Money
from ynab_agent.workflow.overspend_ledger_types import (
    OVERSPEND_LEDGER_WORKFLOW_ID,
    LedgerParams,
    PriorRequest,
    RecordRequest,
)
from ynab_agent.workflow.overspend_ledger_workflow import (
    OverspendLedgerWorkflow,
)
from ynab_agent.workflow.runtime import DATA_CONVERTER

_TASK_QUEUE = "overspend-ledger-test"


async def test_record_then_prior_round_trips_and_scopes() -> None:
    alert = PriorAlert(
        verdict=OverspendVerdict.TRENDING_OVER,
        projected=Money.from_currency("500"),
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[OverspendLedgerWorkflow],
        ),
    ):
        handle = await env.client.start_workflow(
            OverspendLedgerWorkflow.run,
            LedgerParams(),
            id=OVERSPEND_LEDGER_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
        )

        # Fresh ledger → nothing recorded for the category yet.
        assert (
            await handle.query(
                OverspendLedgerWorkflow.prior,
                PriorRequest(category="dining", period="2026-06"),
            )
            is None
        )

        # Record it: the same category+period reads the alert back, hydrated.
        await handle.signal(
            OverspendLedgerWorkflow.record,
            RecordRequest(category="dining", period="2026-06", alert=alert),
        )
        got = await handle.query(
            OverspendLedgerWorkflow.prior,
            PriorRequest(category="dining", period="2026-06"),
        )
        assert got == alert
        assert isinstance(got, PriorAlert)

        # A new month resets the dedupe; a different category is independent.
        assert (
            await handle.query(
                OverspendLedgerWorkflow.prior,
                PriorRequest(category="dining", period="2026-07"),
            )
            is None
        )
        assert (
            await handle.query(
                OverspendLedgerWorkflow.prior,
                PriorRequest(category="gas", period="2026-06"),
            )
            is None
        )


async def test_birth_record_survives_the_run_start() -> None:
    # The monitor signal-with-starts this ledger: the record rides the FIRST
    # workflow task, handled before the run body, so run() must adopt carried
    # state without clobbering that first fold.
    alert = PriorAlert(
        verdict=OverspendVerdict.TRENDING_OVER,
        projected=Money.from_currency("500"),
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[OverspendLedgerWorkflow],
        ),
    ):
        handle = await env.client.start_workflow(
            OverspendLedgerWorkflow.run,
            LedgerParams(),
            id=OVERSPEND_LEDGER_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
            start_signal="record",
            start_signal_args=[
                RecordRequest(category="dining", period="2026-06", alert=alert)
            ],
        )
        got = await handle.query(
            OverspendLedgerWorkflow.prior,
            PriorRequest(category="dining", period="2026-06"),
        )
        assert got == alert
