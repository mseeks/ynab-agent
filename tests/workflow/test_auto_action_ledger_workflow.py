"""The durable auto-action circuit-breaker ledger on the time-skipping server.

Verifies the wiring around the pure folds (tested in ``tests/policy``): the
``record`` signal folds an auto-action in, and the ``counters`` query reads the
per-run / per-day counts back out. The query carries ``now`` so the window maths
line up with the ``workflow.now()`` the signal records at.
"""

from __future__ import annotations

import datetime

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.workflow.auto_action_ledger_workflow import (
    AutoActionLedgerWorkflow,
)
from ynab_agent.workflow.auto_action_types import (
    AUTO_ACTION_LEDGER_WORKFLOW_ID,
    CountersRequest,
    LedgerParams,
)
from ynab_agent.workflow.runtime import DATA_CONVERTER

_TASK_QUEUE = "auto-action-ledger-test"


async def test_record_then_count_and_window() -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AutoActionLedgerWorkflow],
        ),
    ):
        handle = await env.client.start_workflow(
            AutoActionLedgerWorkflow.run,
            LedgerParams(),
            id=AUTO_ACTION_LEDGER_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
        )
        now = await env.get_current_time()

        # Fresh ledger → zero counts.
        empty = await handle.query(
            AutoActionLedgerWorkflow.counters, CountersRequest(now=now)
        )
        assert (empty.this_run, empty.today) == (0, 0)

        # Two auto-actions land; the same txn signalled twice counts once.
        await handle.signal(AutoActionLedgerWorkflow.record, "txn-1")
        await handle.signal(AutoActionLedgerWorkflow.record, "txn-2")
        await handle.signal(AutoActionLedgerWorkflow.record, "txn-1")
        counts = await handle.query(
            AutoActionLedgerWorkflow.counters, CountersRequest(now=now)
        )
        assert (counts.this_run, counts.today) == (2, 2)

        # Two hours on, the run window has rolled off but the day window holds.
        later = now + datetime.timedelta(hours=2)
        rolled = await handle.query(
            AutoActionLedgerWorkflow.counters, CountersRequest(now=later)
        )
        assert (rolled.this_run, rolled.today) == (0, 2)
