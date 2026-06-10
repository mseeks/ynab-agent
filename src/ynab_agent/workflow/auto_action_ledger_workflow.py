"""The durable auto-action circuit-breaker ledger (SPEC §0.6 Layer 1).

A singleton, long-lived workflow (id :data:`AUTO_ACTION_LEDGER_WORKFLOW_ID`)
holding the recent-auto-action tail as Temporal state — the memory that makes
the hard floor's per-run / per-day breaker actually *bind*. Without it the
counters are always zero and the cap can never trip (SPEC §0.6). Born on the
first auto-action — ``record_auto_action`` does a signal-with-start — and
lives forever, continuing-as-new to keep its history bounded. A thin durable
shell over the pure :mod:`ynab_agent.policy.auto_action_ledger` folds, exactly
like ``alert_ledger_workflow`` over ``alert.ledger``:

* the ``counters`` query projects the tail into the floor's counters at a given
  ``now`` without mutating anything;
* the ``record`` signal folds a landed auto-action into the tail.

``now`` travels in with the query (a query cannot read the wall clock); the
signal stamps ``workflow.now`` so replay stays deterministic.
"""

from __future__ import annotations

from temporalio import workflow

from ynab_agent.workflow.auto_action_types import CountersRequest, LedgerParams

with workflow.unsafe.imports_passed_through():
    from ynab_agent.policy.auto_action_ledger import (
        AutoActionLedgerState,
        counters,
        record,
    )
    from ynab_agent.policy.floor import AutoActionCounters


@workflow.defn
class AutoActionLedgerWorkflow:
    """The deployment's one durable auto-action tail (one fold per action)."""

    def __init__(self) -> None:
        """Start empty; the run method adopts any carried-forward state."""
        self._state = AutoActionLedgerState()

    @workflow.run
    async def run(self, params: LedgerParams) -> None:
        """Hold the tail, folding signals until history wants rolling."""
        self._state = params.state
        await workflow.wait_condition(
            lambda: workflow.info().is_continue_as_new_suggested()
        )
        workflow.continue_as_new(LedgerParams(state=self._state))

    @workflow.signal
    def record(self, ynab_id: str) -> None:
        """Record that an auto-action for ``ynab_id`` just landed."""
        self._state = record(self._state, ynab_id, now=workflow.now())

    @workflow.query
    def counters(self, request: CountersRequest) -> AutoActionCounters:
        """Project the tail into the floor's counters at ``request.now``."""
        return counters(self._state, request.now)
