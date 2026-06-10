"""W6 · the overspend monitor — pure projection and alerting (SPEC §7).

Run daily per category: from ``budgeted``/``activity`` and where we are in the
month, project month-end spend by run-rate and decide whether to raise an alert.
v1 is notify-only; the spine here is pure — :func:`assess` ranks a category and
:func:`should_alert` enforces the dedupe (at most one alert per period unless it
materially worsens). The workflow does the I/O (fetch, send, remember).

All money is YNAB-native: ``activity`` is signed (outflows negative), so the
spend magnitude is ``-activity`` when it is an outflow.
"""

from __future__ import annotations

import calendar
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field, model_validator

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.config import HOUSEHOLD_TZ
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money

if TYPE_CHECKING:
    import datetime

# Days in the shortest/longest months — the bounds a month length must fall in.
_MIN_MONTH_DAYS = 28
_MAX_MONTH_DAYS = 31


class CategorySpend(Frozen):
    """A category's month-to-date budget figures (YNAB-native signs)."""

    category: CategoryId
    name: str
    budgeted: Money
    activity: Money
    balance: Money


class MonthClock(Frozen):
    """Where we are in the budget month, for the run-rate projection."""

    day_of_month: int = Field(ge=1)
    days_in_month: int = Field(ge=_MIN_MONTH_DAYS, le=_MAX_MONTH_DAYS)

    @model_validator(mode="after")
    def _check_within_month(self) -> MonthClock:
        if self.day_of_month > self.days_in_month:
            msg = "day_of_month cannot exceed days_in_month"
            raise ValueError(msg)
        return self


class OverspendVerdict(StrEnum):
    """How a category is tracking against its budget this month."""

    OK = "ok"
    TRENDING_OVER = "trending_over"
    ALREADY_OVER = "already_over"


class OverspendPolicy(Frozen):
    """The tunables: the trend threshold, and when trending becomes credible.

    ``min_trend_day`` guards the run-rate's early-month blowup: on day 1 the
    projection is spend x31, so any normal charge reads as "trending over" and
    would fire a false alarm (and a phantom W7 money-moving offer) at every
    month start. Truly over-budget categories still alert on any day —
    ALREADY_OVER is real arithmetic, not a projection.
    """

    trend_threshold: Money = Field(
        default_factory=lambda: Money.from_currency(25)
    )
    min_trend_day: int = Field(default=5, ge=1)


DEFAULT_OVERSPEND_POLICY = OverspendPolicy()


class OverspendAssessment(Frozen):
    """The verdict for one category and the numbers behind it."""

    category: CategoryId
    name: str
    verdict: OverspendVerdict
    budgeted: Money
    spent: Money
    projected: Money


class PriorAlert(Frozen):
    """The last alert raised for a category this period (for dedupe)."""

    verdict: OverspendVerdict
    projected: Money


def spent_magnitude(category: CategorySpend) -> Money:
    """The positive amount spent this month (``-activity`` if an outflow)."""
    if category.activity.is_outflow:
        return -category.activity
    return Money.zero()


def period_and_clock(now: datetime.datetime) -> tuple[str, MonthClock]:
    """The budget period (``YYYY-MM``) and month position, in household time.

    ``now`` is the deterministic UTC instant (``workflow.now()``); it is
    converted to the declared household timezone (SPEC §11, §13) before the
    day and month are read, so a charge near midnight or a month boundary is
    bucketed into the right month and the run-rate's ``day_of_month`` is the
    household's, not UTC's (which is hours ahead).
    """
    local = now.astimezone(HOUSEHOLD_TZ)
    clock = MonthClock(
        day_of_month=local.day,
        days_in_month=calendar.monthrange(local.year, local.month)[1],
    )
    return local.strftime("%Y-%m"), clock


def project_spend(
    category: CategorySpend, clock: MonthClock, scheduled: Money
) -> Money:
    """Project month-end spend by run-rate plus known scheduled outflows (§7).

    ``spent / days_elapsed * days_in_month + scheduled``, in exact milliunits.
    """
    spent = spent_magnitude(category)
    run_rate = Money.from_milliunits(
        spent.milliunits * clock.days_in_month // clock.day_of_month
    )
    return run_rate + scheduled


def assess(
    category: CategorySpend,
    clock: MonthClock,
    *,
    scheduled: Money | None = None,
    policy: OverspendPolicy = DEFAULT_OVERSPEND_POLICY,
) -> OverspendAssessment:
    """Rank a category against its budget for the month (SPEC §7). Pure."""
    scheduled = scheduled or Money.zero()
    spent = spent_magnitude(category)
    projected = project_spend(category, clock, scheduled)

    if spent > category.budgeted:
        verdict = OverspendVerdict.ALREADY_OVER
    elif (
        clock.day_of_month >= policy.min_trend_day
        and projected - category.budgeted > policy.trend_threshold
    ):
        verdict = OverspendVerdict.TRENDING_OVER
    else:
        verdict = OverspendVerdict.OK

    return OverspendAssessment(
        category=category.category,
        name=category.name,
        verdict=verdict,
        budgeted=category.budgeted,
        spent=spent,
        projected=projected,
    )


def should_alert(
    assessment: OverspendAssessment,
    prior: PriorAlert | None,
    *,
    policy: OverspendPolicy = DEFAULT_OVERSPEND_POLICY,
) -> bool:
    """Whether to raise an alert now, deduped against the last one (SPEC §7).

    Never alert when OK; always alert the first flag of a period; otherwise
    re-alert only on a material worsening — an escalation to already-over, or a
    projection that climbed by more than the trend threshold.
    """
    if assessment.verdict is OverspendVerdict.OK:
        return False
    if prior is None:
        return True
    escalated = (
        assessment.verdict is OverspendVerdict.ALREADY_OVER
        and prior.verdict is not OverspendVerdict.ALREADY_OVER
    )
    worsened = assessment.projected - prior.projected > policy.trend_threshold
    return escalated or worsened
