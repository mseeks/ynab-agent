"""W1 · the YNAB Ingestion Poller (SPEC §2, §13).

A short workflow run on a schedule (every ~1-3h). It polls the YNAB transactions
delta, plans which transactions to address via the pure
:func:`~ynab_agent.ingest.plan.plan_ingest`, signal-with-starts a W2 for each,
and advances the cursor. The first run (``cursor is None``) captures the cursor
without acting — the cold-start cutover that avoids emailing about the backlog.

The schedule itself is infrastructure (a Temporal Schedule), configured at
deploy; this is the work each tick performs.
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
    """One tick of the YNAB ingestion poll."""

    @workflow.run
    async def run(self, params: PollParams) -> PollResult:
        """Poll the delta, address in-scope transactions, advance the cursor."""
        cold_start = params.cursor is None
        page = await workflow.execute_activity(
            poll_activities.fetch_delta,
            args=[params.scope.budget_id, params.cursor],
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
        await workflow.execute_activity(
            poll_activities.save_cursor,
            args=[params.scope.budget_id, page.server_knowledge],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        return PollResult(
            addressed=len(actions),
            routed_to_human=sum(1 for a in actions if a.route_to_human),
            new_cursor=page.server_knowledge,
        )
