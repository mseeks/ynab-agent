"""The durable alert-dedup ledger workflow on the time-skipping server.

Verifies the wiring around the pure folds (tested in ``tests/alert``): the
``record`` signal folds an alert in, and the ``should_notify`` query reads the
cooldown back out. Queries use the env's own clock so the cooldown maths line up
with the ``workflow.now()`` the signal records at.
"""

from __future__ import annotations

import datetime

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.workflow.alert_ledger_workflow import AlertLedgerWorkflow
from ynab_agent.workflow.alert_types import (
    ALERT_LEDGER_WORKFLOW_ID,
    LedgerParams,
    ShouldNotifyRequest,
)
from ynab_agent.workflow.runtime import DATA_CONVERTER

_TASK_QUEUE = "alert-ledger-test"


async def test_record_then_dedup_and_recover() -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AlertLedgerWorkflow],
        ),
    ):
        handle = await env.client.start_workflow(
            AlertLedgerWorkflow.run,
            LedgerParams(),
            id=ALERT_LEDGER_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
        )
        now = await env.get_current_time()

        # Fresh ledger → a key notifies.
        assert await handle.query(
            AlertLedgerWorkflow.should_notify,
            ShouldNotifyRequest(key="txn-1", now=now),
        )

        # Record it: the same key is now quiet, a different key still notifies.
        await handle.signal(AlertLedgerWorkflow.record, "txn-1")
        assert not await handle.query(
            AlertLedgerWorkflow.should_notify,
            ShouldNotifyRequest(key="txn-1", now=now),
        )
        assert await handle.query(
            AlertLedgerWorkflow.should_notify,
            ShouldNotifyRequest(key="txn-2", now=now),
        )

        # A day later the cooldown has elapsed and the key notifies again.
        tomorrow = now + datetime.timedelta(hours=25)
        assert await handle.query(
            AlertLedgerWorkflow.should_notify,
            ShouldNotifyRequest(key="txn-1", now=tomorrow),
        )
