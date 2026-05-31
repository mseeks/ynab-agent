"""Worker wiring: the Pydantic data converter and activity/workflow registry.

A real worker (and the workflow tests) constructs its client with
:data:`DATA_CONVERTER` so domain models serialize through Temporal, and
registers :data:`WORKFLOWS` and :data:`ALL_ACTIVITIES`. Tests substitute mock
activity implementations for the stubs.
"""

from __future__ import annotations

from temporalio.contrib.pydantic import pydantic_data_converter

from ynab_agent.workflow import activities
from ynab_agent.workflow.txn_workflow import TransactionWorkflow

DATA_CONVERTER = pydantic_data_converter

WORKFLOWS = [TransactionWorkflow]

ALL_ACTIVITIES = [
    activities.fetch_snapshot,
    activities.enrich,
    activities.commit_to_ynab,
    activities.read_back,
    activities.open_thread,
    activities.send_thread_message,
    activities.interpret_inbound,
    activities.converge,
    activities.feed_rule_learning,
    activities.close_thread,
]
