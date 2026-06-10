"""The durable receipt ledger — W4's parked store (SPEC §6).

A singleton, long-lived workflow (id :data:`RECEIPT_LEDGER_WORKFLOW_ID`)
holding the parked-receipt table as Temporal state. It is born on the first
forwarded receipt — ``route_receipt`` does a signal-with-start — and lives
forever, continuing-as-new to keep its history bounded while carrying the
table forward. A thin durable shell over the pure :mod:`ynab_agent.join.store`
folds, exactly like ``alert_ledger_workflow`` over ``alert.ledger``:

* the ``park`` signal adds a receipt (idempotent on its id, so webhook
  retries and re-forwards never reset a join);
* the ``set_status`` signal moves one through ``parked → matched / asked /
  expired`` (the join's act-once / ask-once dedup state);
* the ``get`` / ``open_receipts`` queries feed ``signal_match``'s facts
  lookup and W1's parked re-check.
"""

from __future__ import annotations

from temporalio import workflow

from ynab_agent.workflow.receipt_ledger_types import (
    ReceiptLedgerParams,
    SetStatusRequest,
)

with workflow.unsafe.imports_passed_through():
    from ynab_agent.domain.receipt import Receipt
    from ynab_agent.join.store import (
        ReceiptLedgerState,
        get,
        open_receipts,
        park,
        set_status,
    )


@workflow.defn
class ReceiptLedgerWorkflow:
    """The household's one parked-receipt table (one fold per signal)."""

    def __init__(self) -> None:
        """Start empty; the run method adopts any carried-forward state."""
        self._state = ReceiptLedgerState()

    @workflow.run
    async def run(self, params: ReceiptLedgerParams) -> None:
        """Hold the table, folding signals until history wants rolling."""
        self._state = params.state
        await workflow.wait_condition(
            lambda: workflow.info().is_continue_as_new_suggested()
        )
        workflow.continue_as_new(ReceiptLedgerParams(state=self._state))

    @workflow.signal
    def park(self, receipt: Receipt) -> None:
        """Add a forwarded receipt to the table (idempotent on its id)."""
        self._state = park(self._state, receipt)

    @workflow.signal
    def set_status(self, request: SetStatusRequest) -> None:
        """Record a join action's resulting status (the dedup state)."""
        self._state = set_status(
            self._state, request.receipt_id, request.status
        )

    @workflow.query
    def get(self, receipt_id: str) -> Receipt | None:
        """The receipt with this id, or ``None``."""
        return get(self._state, receipt_id)

    @workflow.query
    def open_receipts(self) -> tuple[Receipt, ...]:
        """The receipts a re-check should still attempt (parked/asked)."""
        return open_receipts(self._state)
