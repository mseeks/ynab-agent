"""The §13 deadman: page when the W1 poll loop stops, out-of-band.

A short workflow fired hourly by a Temporal Schedule (the trigger is
infrastructure — ``manage/schedule-deadman.yaml``). It probes the poll loop's
liveness (exists, running, ticked recently) and pushes a deduped ntfy page when
something is wrong. This is the *positive*-liveness half of SPEC §13's alerting:
the failure hooks only fire when an error is raised, but a terminated or wedged
loop raises nothing — the silent stop the SPEC calls the most dangerous failure
for a money agent. The schedule fires from the Temporal server, so the check
survives worker restarts and the loop's own death.

The page rides the existing failure-alert path (ntfy + the dedup ledger), so a
down loop pages once per cooldown, not once per hour.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from ynab_agent.workflow import alert_activities, poll_activities
    from ynab_agent.workflow.alert_types import FailureAlert
    from ynab_agent.workflow.constants import (
        ALERT_BUDGET,
        ALERT_RETRY,
        ALERT_TIMEOUT,
    )

_CHECK_TIMEOUT = timedelta(seconds=60)


@workflow.defn
class DeadmanWorkflow:
    """One hourly is-the-poll-alive check, paging on a problem."""

    @workflow.run
    async def run(self) -> str:
        """Probe the poll loop; page (deduped) when it is down or wedged."""
        problem = await workflow.execute_activity(
            poll_activities.check_poll_liveness,
            start_to_close_timeout=_CHECK_TIMEOUT,
            retry_policy=ALERT_RETRY,
        )
        if problem is None:
            return "ok"
        await workflow.execute_activity(
            alert_activities.alert_failure,
            FailureAlert(
                key="deadman-poll",
                title="ynab-agent: ingestion is down",
                body=problem,
            ),
            start_to_close_timeout=ALERT_TIMEOUT,
            schedule_to_close_timeout=ALERT_BUDGET,
            retry_policy=ALERT_RETRY,
        )
        return problem
