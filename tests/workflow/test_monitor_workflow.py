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
from ynab_agent.workflow.monitor_types import MonitorParams, MonitorResult
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
    offered: list[str],
) -> list[Callable[..., object]]:
    @activity.defn(name="fetch_category_spends")
    async def fetch_category_spends() -> list[CategorySpend]:
        return spends

    @activity.defn(name="load_prior_alert")
    async def load_prior_alert(category_id: str) -> PriorAlert | None:
        return prior

    @activity.defn(name="send_overspend_alert")
    async def send_overspend_alert(assessment: OverspendAssessment) -> str:
        sent.append(assessment.name)
        return f"thr-{assessment.category}"

    @activity.defn(name="save_alert")
    async def save_alert(category_id: str, alert: PriorAlert) -> None:
        return None

    @activity.defn(name="start_balance_offer")
    async def start_balance_offer(
        assessment: OverspendAssessment, thread_id: str
    ) -> None:
        offered.append(thread_id)

    return [
        fetch_category_spends,
        load_prior_alert,
        send_overspend_alert,
        save_alert,
        start_balance_offer,
    ]


async def _run(
    *,
    wf_id: str,
    spends: list[CategorySpend],
    prior: PriorAlert | None = None,
) -> tuple[MonitorResult, list[str], list[str]]:
    sent: list[str] = []
    offered: list[str] = []
    acts = _activities(spends=spends, prior=prior, sent=sent, offered=offered)
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
    return result, sent, offered


async def test_overspending_category_is_alerted() -> None:
    # $250 of $400 at mid-month → projects ~$500, trending over.
    result, sent, offered = await _run(
        wf_id="mon-alert",
        spends=[_spend("Dining", budgeted="400", activity="-250")],
    )
    assert result.alerts == 1
    assert result.alerted == ("Dining",)
    assert sent == ["Dining"]
    # The alert hands its thread to a balancing offer (W6→W7, §8).
    assert offered == ["thr-Dining"]


async def test_on_track_category_is_silent() -> None:
    result, sent, offered = await _run(
        wf_id="mon-ok",
        spends=[_spend("Gas", budgeted="400", activity="-100")],
    )
    assert result.alerts == 0
    assert sent == []
    assert offered == []


async def test_duplicate_alert_is_suppressed() -> None:
    # An identical prior alert (same projection) → deduped, no send.
    spends = [_spend("Dining", budgeted="400", activity="-250")]
    prior = PriorAlert(
        verdict=OverspendVerdict.TRENDING_OVER,
        projected=Money.from_currency("500"),
    )
    result, sent, offered = await _run(
        wf_id="mon-dedupe", spends=spends, prior=prior
    )
    assert result.alerts == 0
    assert sent == []
    assert offered == []
