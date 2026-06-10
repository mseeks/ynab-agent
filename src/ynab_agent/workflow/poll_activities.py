"""The I/O ports of the W1 ingestion poller, as Temporal activities.

Kept separate from the W2 :mod:`ynab_agent.workflow.activities` so the W2
workflow's sandbox import graph stays minimal — pulling the ingest/poll types
into the W2 activity module duplicates domain classes under the sandbox and
breaks discriminated-union validation. Heavy clients (YNAB, Temporal) are
imported lazily inside the bodies so they never enter the workflow sandbox.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.ingest.plan import AddressTxn


@activity.defn
async def fetch_unapproved() -> tuple[YnabSnapshot, ...]:
    """Read YNAB's currently unapproved transactions (SPEC §2 W1).

    The agent's outstanding-work set — YNAB's own ``type=unapproved`` view. It
    excludes tentatively scheduled/auto-matched imports until they land. The
    poll re-reads it each tick; the set is small, so there is no cursor to carry
    (what's outstanding is derived from YNAB, not stored — SPEC §0.5).
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    return await asyncio.to_thread(client.unapproved)


@activity.defn
async def address_transaction(action: AddressTxn) -> None:
    """Start the transaction's W2 by ``ynab_id`` (SPEC §2, §3).

    Idempotent by construction: the W2's workflow id *is* the YNAB transaction
    id, so "what's new" is derived from whether a workflow already exists — no
    stored seen-set. The reuse policy is ``ALLOW_DUPLICATE_FAILED_ONLY``: a txn
    already in flight, or one that *completed* (archived — even un-replied),
    raises :class:`WorkflowAlreadyStartedError` and is a no-op, so the agent
    never re-triages settled work; but a run that was *terminated* (an operator
    reset) can be re-created. The W2 reads its own snapshot in ``DISCOVERED``.
    ``route_to_human`` is not yet differentiated in v1 — the autonomy gate
    already routes conservatively — so it is carried for observability only.

    One exception to the no-op: when ``notify_existing`` is set (an Amazon
    item-detail memo has backfilled, SPEC §2/§3), W1 *signals* the
    already-running W2 the fresh snapshot via ``notify_snapshot`` so a
    ``HOLD_AMAZON`` run resolves early instead of waiting out the ~36h deadline.
    Other states ignore the signal; a settled run can no longer be signalled
    (suppressed), and the next tick re-tries while the txn stays unapproved.
    """
    import contextlib

    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.service import RPCError

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
            id_reuse_policy=(WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY),
        )
    except WorkflowAlreadyStartedError:
        if not action.notify_existing:
            return
        handle = temporal.get_workflow_handle(str(ynab_id))
        with contextlib.suppress(RPCError):
            await handle.signal("notify_snapshot", action.snapshot)
