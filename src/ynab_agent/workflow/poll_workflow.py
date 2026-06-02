"""W1 · the YNAB Ingestion Poller (SPEC §2, §13).

A durable poll loop. Each tick reads YNAB's *unapproved* transactions (the
outstanding work), plans which to address via the pure
:func:`~ynab_agent.ingest.plan.plan_ingest`, starts a W2 for each, then sleeps
and continues-as-new. There is no cursor: the outstanding set is YNAB's
``type=unapproved`` view, re-read each tick and derived from YNAB rather than
stored (SPEC §0.5). A transaction approved (by the owner or the agent's own
triage) simply leaves the set; a new import enters it. ``REJECT_DUPLICATE`` on
the per-transaction workflow id makes re-addressing an already-handled one a
no-op.

A one-shot run (``continuous=False``, the default) performs a single tick and
returns its :class:`PollResult` — what tests and a manual kick use. Production
starts it once with ``continuous=True`` and it runs forever.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from ynab_agent.ingest.plan import plan_ingest
    from ynab_agent.workflow import poll_activities
    from ynab_agent.workflow.constants import ACTIVITY_RETRY
    from ynab_agent.workflow.poll_types import PollParams, PollResult

_ACTIVITY_TIMEOUT = timedelta(seconds=60)


@workflow.defn
class PollWorkflow:
    """The YNAB ingestion poll loop (one tick per continue-as-new)."""

    @workflow.run
    async def run(self, params: PollParams) -> PollResult:
        """Read the unapproved set, address it, then loop or return."""
        snapshots = await workflow.execute_activity(
            poll_activities.fetch_unapproved,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        # The fetched set is already unapproved; the scope bounds it by install
        # date + account, and cold_start is moot (no backlog to suppress — the
        # outstanding set IS the work).
        actions = plan_ingest(snapshots, params.scope, cold_start=False)
        for action in actions:
            await workflow.execute_activity(
                poll_activities.address_transaction,
                action,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
        result = PollResult(
            addressed=len(actions),
            routed_to_human=sum(1 for a in actions if a.route_to_human),
        )
        if not params.continuous:
            return result
        # Durable loop: sleep, then restart fresh. continue_as_new raises, so
        # nothing runs after it and history stays bounded to one tick.
        await workflow.sleep(timedelta(seconds=params.interval_seconds))
        workflow.continue_as_new(
            PollParams(
                scope=params.scope,
                interval_seconds=params.interval_seconds,
                continuous=True,
            )
        )
