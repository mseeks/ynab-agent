"""Tests for the worker entrypoint's runtime assembly."""

from __future__ import annotations

from ynab_agent.worker import DEFAULT_TASK_QUEUE, run_worker
from ynab_agent.workflow.runtime import ALL_ACTIVITIES, WORKFLOWS
from ynab_agent.workflow.txn_workflow import TransactionWorkflow


def test_runtime_registry_is_wired() -> None:
    # The five workflow classes (W2/W1/W3/W4/W6) and all activity ports.
    assert TransactionWorkflow in WORKFLOWS
    assert len(WORKFLOWS) == 5
    assert len(ALL_ACTIVITIES) >= 20
    assert callable(run_worker)
    assert DEFAULT_TASK_QUEUE
