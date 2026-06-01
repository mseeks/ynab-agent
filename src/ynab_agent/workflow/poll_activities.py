"""The I/O ports of the W1 ingestion poller, as Temporal activities.

Kept separate from the W2 :mod:`ynab_agent.workflow.activities` so the W2
workflow's sandbox import graph stays minimal — pulling the ingest/poll types
into the W2 activity module duplicates domain classes under the sandbox and
breaks discriminated-union validation. Heavy clients (YNAB, Temporal) are
imported lazily inside the bodies so they never enter the workflow sandbox.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.ingest.plan import AddressTxn
from ynab_agent.ingest.scope import IngestScope
from ynab_agent.workflow.poll_types import DeltaPage


@activity.defn
async def fetch_delta(scope: IngestScope, cursor: int | None) -> DeltaPage:
    """Poll the YNAB transactions delta from ``cursor`` (SPEC §2 W1).

    The fetch is bounded by ``scope.install_date`` so a cold start (``cursor``
    is ``None``) never pulls years of history; once warm, ``cursor`` is YNAB's
    ``server_knowledge`` and only changed transactions come back. The advanced
    cursor rides home in the page — the W1 workflow carries it forward across
    continue-as-new (no stored cursor, SPEC §0.5).
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    snapshots, server_knowledge = await asyncio.to_thread(
        client.delta, scope.install_date, cursor
    )
    return DeltaPage(snapshots=snapshots, server_knowledge=server_knowledge)


@activity.defn
async def address_transaction(action: AddressTxn) -> None:
    """Start the transaction's W2 by ``ynab_id`` (SPEC §2, §3).

    Idempotent by construction: the W2's workflow id *is* the YNAB transaction
    id, started ``REJECT_DUPLICATE``, so "what's new" is derived from whether a
    workflow already exists — no stored seen-set. A txn already in flight (or
    archived) raises :class:`WorkflowAlreadyStartedError`, which is the no-op
    signal. The W2 reads its own snapshot in ``DISCOVERED``. ``route_to_human``
    is not yet differentiated in v1 — the autonomy gate already routes
    conservatively — so it is carried for observability only.
    """
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ynab_agent.workflow.temporal_client import client, task_queue
    from ynab_agent.workflow.types import TransactionParams

    ynab_id = action.snapshot.ynab_id
    temporal = await client()
    try:
        await temporal.start_workflow(
            "TransactionWorkflow",
            TransactionParams(ynab_id=ynab_id),
            id=str(ynab_id),
            task_queue=task_queue(),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return
