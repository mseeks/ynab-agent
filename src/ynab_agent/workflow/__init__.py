"""The Temporal runtime layer: the W2 lifecycle workflow and its I/O ports.

This is the deterministic spine. The workflow drives the pure domain core; the
activities (:mod:`ynab_agent.workflow.activities`) are the I/O ports, stubbed
until the MCP / Pydantic AI implementations are wired in.
"""

from __future__ import annotations

from ynab_agent.workflow.runtime import (
    ALL_ACTIVITIES,
    DATA_CONVERTER,
    WORKFLOWS,
)
from ynab_agent.workflow.txn_workflow import TransactionWorkflow
from ynab_agent.workflow.types import (
    AnswerOutcome,
    ClarifyOutcome,
    ReplyOutcome,
    TransactionParams,
)

__all__ = [
    "ALL_ACTIVITIES",
    "DATA_CONVERTER",
    "WORKFLOWS",
    "AnswerOutcome",
    "ClarifyOutcome",
    "ReplyOutcome",
    "TransactionParams",
    "TransactionWorkflow",
]
