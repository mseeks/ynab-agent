"""The durable receipt ledger holds the parked table across signals (§6)."""

from __future__ import annotations

import datetime

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.ids import ReceiptId
from ynab_agent.domain.receipt import Receipt
from ynab_agent.workflow.receipt_ledger_types import (
    RECEIPT_LEDGER_WORKFLOW_ID,
    ReceiptLedgerParams,
    SetStatusRequest,
)
from ynab_agent.workflow.receipt_ledger_workflow import ReceiptLedgerWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

_TASK_QUEUE = "receipt-ledger-test"
_NOW = datetime.datetime(2026, 6, 10, 12, 0, tzinfo=datetime.UTC)


def _receipt(rid: str) -> Receipt:
    return Receipt(id=ReceiptId(rid), parked_at=_NOW, merchant="Whole Foods")


async def test_ledger_parks_dedups_and_tracks_status() -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[ReceiptLedgerWorkflow],
        ),
    ):
        handle = await env.client.start_workflow(
            ReceiptLedgerWorkflow.run,
            ReceiptLedgerParams(),
            id=RECEIPT_LEDGER_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
        )
        await handle.signal(ReceiptLedgerWorkflow.park, _receipt("r1"))
        await handle.signal(ReceiptLedgerWorkflow.park, _receipt("r2"))

        open_now = await handle.query(ReceiptLedgerWorkflow.open_receipts)
        assert {str(r.id) for r in open_now} == {"r1", "r2"}

        got = await handle.query(ReceiptLedgerWorkflow.get, "r1")
        assert got is not None
        assert got.merchant == "Whole Foods"

        await handle.signal(
            ReceiptLedgerWorkflow.set_status,
            SetStatusRequest(receipt_id="r1", status=ReceiptStatus.MATCHED),
        )
        # A webhook retry re-parks r1 — the MATCHED status must survive.
        await handle.signal(ReceiptLedgerWorkflow.park, _receipt("r1"))

        open_after = await handle.query(ReceiptLedgerWorkflow.open_receipts)
        assert {str(r.id) for r in open_after} == {"r2"}
        kept = await handle.query(ReceiptLedgerWorkflow.get, "r1")
        assert kept is not None
        assert kept.status is ReceiptStatus.MATCHED
