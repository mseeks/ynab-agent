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
    from ynab_agent.workflow import balance_activities, monitor_activities
    from ynab_agent.workflow.constants import ACTIVITY_RETRY, ACTIVITY_TIMEOUT
    from ynab_agent.workflow.monitor_types import MonitorParams, MonitorResult


@workflow.defn
class OverspendMonitorWorkflow:
    """One daily overspend pass across all categories."""

    @workflow.run
    async def run(self, params: MonitorParams) -> MonitorResult:
        """Assess every category and alert on what is off track (SPEC §7)."""
        # Read the deterministic clock once and derive the period from it, then
        # pass that period into every activity in the pass — so the thread, the
        # dedupe key, and the W7 offer id can't drift across a month boundary
        # (SPEC §0.5: clocks via ``workflow.now()``, decided in the workflow).
        now = workflow.now()
        period = now.strftime("%Y-%m")
        clock = params.clock or MonthClock(
            day_of_month=now.day,
            days_in_month=calendar.monthrange(now.year, now.month)[1],
        )
        spends = await workflow.execute_activity(
            monitor_activities.fetch_category_spends,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )

        alerted: list[str] = []
        for spend in spends:
            assessment = assess(spend, clock)
            if assessment.verdict is OverspendVerdict.OK:
                continue
            prior = await workflow.execute_activity(
                monitor_activities.load_prior_alert,
                args=[str(spend.category), period],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            if not should_alert(assessment, prior):
                continue
            thread_id = await workflow.execute_activity(
                monitor_activities.send_overspend_alert,
                args=[assessment, period],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            await workflow.execute_activity(
                monitor_activities.save_alert,
                args=[
                    str(spend.category),
                    PriorAlert(
                        verdict=assessment.verdict,
                        projected=assessment.projected,
                    ),
                    period,
                ],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            # Offer a balancing move on the same alert thread (W6→W7, §8). A
            # fire-and-forget start: REJECT_DUPLICATE keeps it one offer per
            # category per period, and the monitor never waits on coverage.
            await workflow.execute_activity(
                balance_activities.start_balance_offer,
                args=[assessment, thread_id, period],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            alerted.append(spend.name)

        return MonitorResult(
            categories=len(spends),
            alerts=len(alerted),
            alerted=tuple(alerted),
        )
