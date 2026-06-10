"""Value types for the durable receipt ledger (W4's parked store, SPEC §6)."""

from __future__ import annotations

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.join.store import ReceiptLedgerState

RECEIPT_LEDGER_WORKFLOW_ID = "ynab-receipt-ledger"


class ReceiptLedgerParams(Frozen):
    """The ledger workflow's params: the state to carry forward."""

    state: ReceiptLedgerState = ReceiptLedgerState()


class SetStatusRequest(Frozen):
    """Move one receipt to a new join status (the ``set_status`` signal)."""

    receipt_id: str
    status: ReceiptStatus
