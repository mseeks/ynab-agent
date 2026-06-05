"""The durable alert-dedup ledger — the memory that keeps alerts from flooding.

A singleton, long-lived workflow (id :data:`ALERT_LEDGER_WORKFLOW_ID`) holding
the recent-alert tail as Temporal state (SPEC §13). It is born on the first
alert — the ``alert_failure`` activity does a signal-with-start — and lives
forever, continuing-as-new to keep its history bounded while carrying the tail
forward. A thin durable shell over the pure :mod:`ynab_agent.alert.ledger`
folds, exactly like ``registry_workflow`` over ``learn.registry``:

* the ``should_notify`` query answers "would an alert for this key fire now?"
  (per-key cooldown + global rate cap) without mutating anything;
* the ``record`` signal folds a fired alert into the tail.

All clock reads go through ``workflow.now`` so replay stays deterministic.
"""

from __future__ import annotations

from temporalio import workflow

from ynab_agent.workflow.alert_types import LedgerParams, ShouldNotifyRequest

with workflow.unsafe.imports_passed_through():
    from ynab_agent.alert.ledger import LedgerState, record, should_notify


@workflow.defn
class AlertLedgerWorkflow:
    """The deployment's one durable alert-dedup tail (one fold per alert)."""

    def __init__(self) -> None:
        """Start empty; the run method adopts any carried-forward state."""
        self._state = LedgerState()

    @workflow.run
    async def run(self, params: LedgerParams) -> None:
        """Hold the tail, folding signals until history wants rolling."""
        self._state = params.state
        await workflow.wait_condition(
            lambda: workflow.info().is_continue_as_new_suggested()
        )
        workflow.continue_as_new(LedgerParams(state=self._state))

    @workflow.signal
    def record(self, key: str) -> None:
        """Record that an alert for ``key`` just fired (SPEC §13)."""
        self._state = record(self._state, key, now=workflow.now())

    @workflow.query
    def should_notify(self, request: ShouldNotifyRequest) -> bool:
        """Whether an alert for the key should fire (dedup + rate cap)."""
        return should_notify(self._state, request.key, request.now)
