"""Worker wiring: the Pydantic data converter and activity/workflow registry.

A real worker (and the workflow tests) constructs its client with
:data:`DATA_CONVERTER` so domain models serialize through Temporal, and
registers :data:`WORKFLOWS` and :data:`ALL_ACTIVITIES`. Tests substitute mock
activity implementations for the stubs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio.contrib.pydantic import pydantic_data_converter

from ynab_agent.workflow import (
    activities,
    alert_activities,
    balance_activities,
    dispatch_activities,
    monitor_activities,
    offer_activities,
    poll_activities,
    receipt_activities,
)

if TYPE_CHECKING:
    from collections.abc import Callable
from ynab_agent.workflow.alert_ledger_workflow import AlertLedgerWorkflow
from ynab_agent.workflow.balance_workflow import BudgetBalanceWorkflow
from ynab_agent.workflow.dispatch_workflow import DispatchWorkflow
from ynab_agent.workflow.monitor_workflow import OverspendMonitorWorkflow
from ynab_agent.workflow.offer_workflow import AutonomyOfferWorkflow
from ynab_agent.workflow.overspend_ledger_workflow import (
    OverspendLedgerWorkflow,
)
from ynab_agent.workflow.poll_workflow import PollWorkflow
from ynab_agent.workflow.receipt_workflow import ReceiptJoinWorkflow
from ynab_agent.workflow.registry_workflow import RuleRegistryWorkflow
from ynab_agent.workflow.txn_workflow import TransactionWorkflow

DATA_CONVERTER = pydantic_data_converter

WORKFLOWS = [
    TransactionWorkflow,
    PollWorkflow,
    DispatchWorkflow,
    ReceiptJoinWorkflow,
    OverspendMonitorWorkflow,
    RuleRegistryWorkflow,
    AlertLedgerWorkflow,
    AutonomyOfferWorkflow,
    OverspendLedgerWorkflow,
    BudgetBalanceWorkflow,
]

ALL_ACTIVITIES: list[Callable[..., object]] = [
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
    poll_activities.fetch_unapproved,
    poll_activities.address_transaction,
    dispatch_activities.resolve_thread,
    dispatch_activities.resolve_offer_thread,
    dispatch_activities.resolve_balance_thread,
    dispatch_activities.classify_inbound,
    dispatch_activities.signal_transaction,
    dispatch_activities.signal_offer,
    dispatch_activities.signal_balance,
    dispatch_activities.route_receipt,
    dispatch_activities.handle_command,
    offer_activities.start_autonomy_offer,
    offer_activities.open_offer_thread,
    offer_activities.interpret_offer_reply,
    offer_activities.accept_offer,
    offer_activities.decline_offer,
    receipt_activities.match_receipt,
    receipt_activities.signal_match,
    receipt_activities.ask_disambiguation,
    receipt_activities.ask_no_match,
    receipt_activities.save_receipt_status,
    monitor_activities.current_period,
    monitor_activities.fetch_category_spends,
    monitor_activities.load_prior_alert,
    monitor_activities.send_overspend_alert,
    monitor_activities.save_alert,
    balance_activities.start_balance_offer,
    balance_activities.propose_balance_options,
    balance_activities.interpret_balance_reply,
    balance_activities.read_budget_state,
    balance_activities.set_category_budgeted,
    balance_activities.log_budget_moves,
    balance_activities.send_balance_email,
    alert_activities.alert_failure,
]
