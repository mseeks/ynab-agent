"""W6 · the durable overspend-alert dedup ledger (SPEC §7).

A singleton, long-lived workflow (id :data:`OVERSPEND_LEDGER_WORKFLOW_ID`)
holding the per-category last-alert table as Temporal state (SPEC §0.5
derived-state). It is born on the first alert — the ``save_alert`` activity does
a signal-with-start — and lives forever, continuing-as-new to keep its history
bounded while carrying the table forward. A thin durable shell over the pure
:mod:`ynab_agent.budget.ledger` folds, exactly like ``alert_ledger_workflow``
over ``alert.ledger``:

* the ``prior`` query answers "what did we last alert this category, this
  period?" without mutating anything;
* the ``record`` signal folds a fired alert into the table.

The period travels in with each request, so the workflow needs no clock and
replay is trivially deterministic.
"""

from __future__ import annotations

from temporalio import workflow

from ynab_agent.workflow.overspend_ledger_types import (
    LedgerParams,
    PriorRequest,
    RecordRequest,
)

with workflow.unsafe.imports_passed_through():
    from ynab_agent.budget.ledger import prior, record
    from ynab_agent.budget.overspend import PriorAlert


@workflow.defn
class OverspendLedgerWorkflow:
    """The household's one durable overspend-alert dedup table."""

    @workflow.init
    def __init__(self, params: LedgerParams) -> None:
        """Adopt carried-forward state before any signal handler runs.

        Adoption must happen here, not in ``run``: a signal-with-start's
        signal is handled *before* the run method body, so assigning
        ``params.state`` there would drop the birth alert from the table.
        """
        self._state = params.state

    @workflow.run
    async def run(self, _params: LedgerParams) -> None:
        """Hold the table, folding signals until history wants rolling."""
        await workflow.wait_condition(
            lambda: workflow.info().is_continue_as_new_suggested()
        )
        workflow.continue_as_new(LedgerParams(state=self._state))

    @workflow.signal
    def record(self, request: RecordRequest) -> None:
        """Fold a freshly-sent alert into the table (SPEC §7)."""
        self._state = record(
            self._state, request.category, request.period, request.alert
        )

    @workflow.query
    def prior(self, request: PriorRequest) -> PriorAlert | None:
        """The category's last alert this period, for the dedupe."""
        return prior(self._state, request.category, request.period)
