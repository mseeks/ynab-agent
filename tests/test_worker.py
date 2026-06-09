"""Tests for the worker entrypoint's runtime assembly."""

from __future__ import annotations

from ynab_agent.worker import DEFAULT_TASK_QUEUE, run_worker
from ynab_agent.workflow.runtime import ALL_ACTIVITIES, WORKFLOWS
from ynab_agent.workflow.txn_workflow import TransactionWorkflow


def test_runtime_registry_is_wired() -> None:
    # The eleven workflow classes (W2/W1/W3/W4/W6 + the W5 rule registry + the
    # failure-alert ledger + the autonomy-offer workflow + the W6 overspend
    # dedup ledger + the W7 budget-balance workflow + the command-confirm
    # workflow) and all activity ports.
    assert TransactionWorkflow in WORKFLOWS
    assert len(WORKFLOWS) == 11
    assert len(ALL_ACTIVITIES) >= 20
    assert callable(run_worker)
    assert DEFAULT_TASK_QUEUE
