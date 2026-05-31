"""The I/O ports of the W1 ingestion poller, as Temporal activities.

Kept separate from the W2 :mod:`ynab_agent.workflow.activities` so the W2
workflow's sandbox import graph stays minimal — pulling the ingest/poll types
into the W2 activity module duplicates domain classes under the sandbox and
breaks discriminated-union validation. Stubbed like the W2 ports.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.ingest.plan import AddressTxn
from ynab_agent.workflow.poll_types import DeltaPage

_STUB = "workflow activity stub — register a real or mock implementation"


@activity.defn
async def fetch_delta(budget_id: str, cursor: int | None) -> DeltaPage:
    """Poll the YNAB transactions delta from ``cursor`` (SPEC §2 W1)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def address_transaction(action: AddressTxn) -> None:
    """Signal-with-start the transaction's W2 by ``ynab_id`` (SPEC §2, §3)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def save_cursor(budget_id: str, server_knowledge: int) -> None:
    """Persist the advanced delta cursor."""
    raise NotImplementedError(_STUB)
