"""W1 · the YNAB Ingestion Poller (SPEC §2, §13).

A durable poll loop. Each tick reads YNAB's *unapproved* transactions (the
outstanding work), plans which to address via the pure
:func:`~ynab_agent.ingest.plan.plan_ingest`, starts a W2 for each, then sleeps
and continues-as-new. There is no cursor: the outstanding set is YNAB's
``type=unapproved`` view, re-read each tick and derived from YNAB rather than
stored (SPEC §0.5). A transaction approved (by the owner or the agent's own
triage) simply leaves the set; a new import enters it.
``ALLOW_DUPLICATE_FAILED_ONLY`` on the per-transaction workflow id makes
re-addressing an already-running or completed one a no-op, while letting a
*failed* W2 re-fire — so a transient failure self-heals on a later tick.

A one-shot run (``continuous=False``, the default) performs a single tick and
returns its :class:`PollResult` — what tests and a manual kick use. Production
starts it once with ``continuous=True`` and it runs forever.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from ynab_agent.ingest.plan import plan_ingest
    from ynab_agent.workflow import alert_activities, poll_activities
    from ynab_agent.workflow.alerting import build_failure_alert
    from ynab_agent.workflow.constants import (
        ACTIVITY_RETRY,
        ALERT_BUDGET,
        ALERT_RETRY,
        ALERT_TIMEOUT,
    )
    from ynab_agent.workflow.poll_types import PollParams, PollResult

_ACTIVITY_TIMEOUT = timedelta(seconds=60)


@workflow.defn
class PollWorkflow:
    """The YNAB ingestion poll loop (one tick per continue-as-new)."""

    @workflow.run
    async def run(self, params: PollParams) -> PollResult:
        """Read the unapproved set, address it, then loop or return.

        The continuous loop must outlive any outage: a tick whose activities
        exhaust their retries (~15 min of YNAB/Temporal being unreachable) used
        to fail the whole workflow — permanently and silently ending ingestion,
        the exact "silent stop" SPEC §13 calls the most dangerous failure. Now a
        failed tick pages the operator (deduped, best-effort) and is skipped;
        the loop sleeps and re-fires, and nothing is lost — the unapproved set
        is re-derived from YNAB on every tick. A one-shot run still raises, so
        tests and manual kicks see the real error.
        """
        try:
            result = await self._tick(params)
        except ActivityError as exc:
            if not params.continuous:
                raise
            await workflow.execute_activity(
                alert_activities.alert_failure,
                build_failure_alert(
                    key="w1-poll-tick",
                    context="W1 poll tick",
                    exc=exc,
                ),
                start_to_close_timeout=ALERT_TIMEOUT,
                schedule_to_close_timeout=ALERT_BUDGET,
                retry_policy=ALERT_RETRY,
            )
            result = PollResult(addressed=0, routed_to_human=0)
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

    async def _tick(self, params: PollParams) -> PollResult:
        """One poll pass: read the unapproved set and address each txn."""
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
        return PollResult(
            addressed=len(actions),
            routed_to_human=sum(1 for a in actions if a.route_to_human),
        )
