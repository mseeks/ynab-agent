"""Worker wiring: the Pydantic data converter and activity/workflow registry.

A real worker (and the workflow tests) constructs its client with
:data:`DATA_CONVERTER` so domain models serialize through Temporal, and
registers :data:`WORKFLOWS` and :data:`ALL_ACTIVITIES`. Tests substitute mock
activity implementations for the stubs.
"""

from __future__ import annotations

from temporalio.contrib.pydantic import pydantic_data_converter

from ynab_agent.workflow import (
    activities,
    dispatch_activities,
    poll_activities,
    receipt_activities,
)
from ynab_agent.workflow.dispatch_workflow import DispatchWorkflow
from ynab_agent.workflow.poll_workflow import PollWorkflow
from ynab_agent.workflow.receipt_workflow import ReceiptJoinWorkflow
from ynab_agent.workflow.txn_workflow import TransactionWorkflow

DATA_CONVERTER = pydantic_data_converter

WORKFLOWS = [
    TransactionWorkflow,
    PollWorkflow,
    DispatchWorkflow,
    ReceiptJoinWorkflow,
]

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
    poll_activities.fetch_delta,
    poll_activities.address_transaction,
    poll_activities.save_cursor,
    dispatch_activities.resolve_thread,
    dispatch_activities.classify_inbound,
    dispatch_activities.signal_transaction,
    dispatch_activities.route_receipt,
    dispatch_activities.handle_command,
    receipt_activities.match_receipt,
    receipt_activities.signal_match,
    receipt_activities.ask_disambiguation,
    receipt_activities.ask_no_match,
    receipt_activities.save_receipt_status,
]
