"""W1 · the YNAB Ingestion Poller (SPEC §2, §13).

A durable poll loop. Each tick polls the YNAB transactions delta, plans which
transactions to address via the pure
:func:`~ynab_agent.ingest.plan.plan_ingest`, starts a W2 for each, then sleeps
and continues-as-new carrying the advanced cursor in workflow state — so the
delta cursor is durable without any external store (SPEC §0.5). The first tick
(``cursor is None``) captures the cursor without acting: the cold-start cutover
that avoids emailing the backlog.

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
    from ynab_agent.workflow.poll_types import PollParams, PollResult

_ACTIVITY_TIMEOUT = timedelta(seconds=60)


@workflow.defn
class PollWorkflow:
    """The YNAB ingestion poll loop (one tick per continue-as-new)."""

    @workflow.run
    async def run(self, params: PollParams) -> PollResult:
        """Poll the delta, address in-scope txns, then loop or return."""
        cold_start = params.cursor is None
        page = await workflow.execute_activity(
            poll_activities.fetch_delta,
            args=[params.scope, params.cursor],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        actions = plan_ingest(
            page.snapshots, params.scope, cold_start=cold_start
        )
        for action in actions:
            await workflow.execute_activity(
                poll_activities.address_transaction,
                action,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )
        result = PollResult(
            addressed=len(actions),
            routed_to_human=sum(1 for a in actions if a.route_to_human),
            new_cursor=page.server_knowledge,
        )
        if not params.continuous:
            return result
        # Durable loop: sleep, then restart fresh carrying the advanced cursor
        # in state (the store-free delta cursor). continue_as_new raises, so
        # nothing runs after it and history stays bounded to one tick.
        await workflow.sleep(timedelta(seconds=params.interval_seconds))
        workflow.continue_as_new(
            PollParams(
                scope=params.scope,
                cursor=page.server_knowledge,
                interval_seconds=params.interval_seconds,
                continuous=True,
            )
        )
