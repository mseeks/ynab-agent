"""End-to-end tests for the W6 overspend monitor (time-skipping server)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.budget.overspend import (
    CategorySpend,
    MonthClock,
    OverspendAssessment,
    OverspendVerdict,
    PriorAlert,
)
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money
from ynab_agent.workflow.monitor_types import (
    MonitorParams,
    MonitorResult,
    PeriodClock,
)
from ynab_agent.workflow.monitor_workflow import OverspendMonitorWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

TASK_QUEUE = "ynab-monitor-test"
# Mid-month: a run-rate doubles the month-to-date spend.
_CLOCK = MonthClock(day_of_month=15, days_in_month=30)


def _spend(name: str, *, budgeted: str, activity: str) -> CategorySpend:
    return CategorySpend(
        category=CategoryId(name),
        name=name,
        budgeted=Money.from_currency(budgeted),
        activity=Money.from_currency(activity),
        balance=Money.from_currency(budgeted) + Money.from_currency(activity),
    )


def _activities(
    *,
    spends: list[CategorySpend],
    prior: PriorAlert | None,
    sent: list[str],
    coordinated: list[int],
    periods: list[str],
) -> list[Callable[..., object]]:
    @activity.defn(name="current_period")
    async def current_period() -> PeriodClock:
        return PeriodClock(period="2026-06", clock=_CLOCK)

    @activity.defn(name="fetch_category_spends")
    async def fetch_category_spends(
        period: str, clock: MonthClock
    ) -> list[CategorySpend]:
        return spends

    @activity.defn(name="load_prior_alert")
    async def load_prior_alert(
        category_id: str, period: str
    ) -> PriorAlert | None:
        periods.append(period)
        return prior

    @activity.defn(name="send_overspend_alert")
    async def send_overspend_alert(
        assessment: OverspendAssessment, period: str
    ) -> str:
        periods.append(period)
        sent.append(assessment.name)
        return f"thr-{assessment.category}"

    @activity.defn(name="save_alert")
    async def save_alert(
        category_id: str, alert: PriorAlert, period: str
    ) -> None:
        periods.append(period)

    @activity.defn(name="start_coordinated_balance")
    async def start_coordinated_balance(
        assessments: list[OverspendAssessment], period: str
    ) -> None:
        periods.append(period)
        coordinated.append(len(assessments))

    return [
        current_period,
        fetch_category_spends,
        load_prior_alert,
        send_overspend_alert,
        save_alert,
        start_coordinated_balance,
    ]


async def _run(
    *,
    wf_id: str,
    spends: list[CategorySpend],
    prior: PriorAlert | None = None,
) -> tuple[MonitorResult, list[str], list[int], list[str]]:
    sent: list[str] = []
    coordinated: list[int] = []
    periods: list[str] = []
    acts = _activities(
        spends=spends,
        prior=prior,
        sent=sent,
        coordinated=coordinated,
        periods=periods,
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[OverspendMonitorWorkflow],
            activities=acts,
        ),
    ):
        result = await env.client.execute_workflow(
            OverspendMonitorWorkflow.run,
            MonitorParams(clock=_CLOCK),
            id=wf_id,
            task_queue=TASK_QUEUE,
        )
    return result, sent, coordinated, periods


async def test_overspending_category_is_alerted() -> None:
    # $250 of $400 at mid-month → projects ~$500, trending over.
    result, sent, coordinated, periods = await _run(
        wf_id="mon-alert",
        spends=[_spend("Dining", budgeted="400", activity="-250")],
    )
    assert result.alerts == 1
    assert result.alerted == ("Dining",)
    assert sent == ["Dining"]
    # One coordinated coverage offer per pass over a shared pool (W6→W7, #46),
    # covering the single over category here.
    assert coordinated == [1]
    # One period, computed once in the workflow, reaches every activity in the
    # pass (load + send + save + coordinate) — no per-activity wall-clock drift.
    import re

    assert len(periods) == 4
    assert len(set(periods)) == 1
    assert re.fullmatch(r"\d{4}-\d{2}", periods[0])


async def test_on_track_category_is_silent() -> None:
    result, sent, coordinated, _ = await _run(
        wf_id="mon-ok",
        spends=[_spend("Gas", budgeted="400", activity="-100")],
    )
    assert result.alerts == 0
    assert sent == []
    assert coordinated == []  # nothing over → no coverage offer


async def test_duplicate_alert_is_suppressed_but_coverage_coordinates() -> None:
    # An identical prior alert (same blended projection) → no fresh alert email,
    # but the category is still over, so it still feeds the coordinated
    # coverage.
    spends = [_spend("Dining", budgeted="400", activity="-250")]
    prior = PriorAlert(
        verdict=OverspendVerdict.TRENDING_OVER,
        projected=Money.from_currency("450"),
    )
    result, sent, coordinated, _ = await _run(
        wf_id="mon-dedupe", spends=spends, prior=prior
    )
    assert result.alerts == 0
    assert sent == []
    assert coordinated == [1]  # deduped alert, but coverage still coordinates
