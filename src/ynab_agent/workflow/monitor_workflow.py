"""W6 · the overspend-monitor workflow (SPEC §7).

A short workflow per monitor pass — fired by a daily Temporal Schedule (the
trigger is infrastructure). It reads each category's figures, runs the pure
:func:`~ynab_agent.budget.overspend.assess` projection, and for anything off
track that survives the dedupe (``should_alert`` against the last alert) emails
an alert and records it. v1 is notify-only; no budget is moved (that is W7).

The month position comes from ``params.clock`` when set, else from
``workflow.now()`` plus ``calendar.monthrange`` (both replay-deterministic).
"""

from __future__ import annotations

import calendar

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from ynab_agent.budget.overspend import (
        MonthClock,
        OverspendVerdict,
        PriorAlert,
        assess,
        should_alert,
    )
    from ynab_agent.workflow import monitor_activities
    from ynab_agent.workflow.constants import (
        ACTIVITY_BUDGET,
        ACTIVITY_RETRY,
        ACTIVITY_TIMEOUT,
    )
    from ynab_agent.workflow.monitor_types import MonitorParams, MonitorResult


@workflow.defn
class OverspendMonitorWorkflow:
    """One daily overspend pass across all categories."""

    @workflow.run
    async def run(self, params: MonitorParams) -> MonitorResult:
        """Assess every category and alert on what is off track (SPEC §7)."""
        clock = params.clock or self._current_clock()
        spends = await workflow.execute_activity(
            monitor_activities.fetch_category_spends,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
            schedule_to_close_timeout=ACTIVITY_BUDGET,
        )

        alerted: list[str] = []
        for spend in spends:
            assessment = assess(spend, clock)
            if assessment.verdict is OverspendVerdict.OK:
                continue
            prior = await workflow.execute_activity(
                monitor_activities.load_prior_alert,
                str(spend.category),
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
                schedule_to_close_timeout=ACTIVITY_BUDGET,
            )
            if not should_alert(assessment, prior):
                continue
            await workflow.execute_activity(
                monitor_activities.send_overspend_alert,
                assessment,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
                schedule_to_close_timeout=ACTIVITY_BUDGET,
            )
            await workflow.execute_activity(
                monitor_activities.save_alert,
                args=[
                    str(spend.category),
                    PriorAlert(
                        verdict=assessment.verdict,
                        projected=assessment.projected,
                    ),
                ],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
                schedule_to_close_timeout=ACTIVITY_BUDGET,
            )
            alerted.append(spend.name)

        return MonitorResult(
            categories=len(spends),
            alerts=len(alerted),
            alerted=tuple(alerted),
        )

    def _current_clock(self) -> MonthClock:
        """Derive the month position from the replay-safe workflow clock."""
        now = workflow.now()
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        return MonthClock(day_of_month=now.day, days_in_month=days_in_month)
