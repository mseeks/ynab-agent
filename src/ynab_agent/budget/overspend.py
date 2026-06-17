"""W6 · the overspend monitor — pure projection and alerting (SPEC §7).

Run daily per category: from ``budgeted``/``activity`` and where we are in the
month, project month-end spend with a budget-anchored blend and decide whether
to raise an alert.
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
    """A category's month-to-date budget figures (YNAB-native signs).

    ``balance`` is YNAB's *available* (``budgeted + carryover + activity``), so
    rollover is already baked in. ``scheduled_remaining`` is the known future
    outflow due this month (from the scheduled-transactions read), added to the
    projection at full size; it is 0 when nothing is scheduled.
    """

    category: CategoryId
    name: str
    budgeted: Money
    activity: Money
    balance: Money
    scheduled_remaining: Money = Field(default_factory=Money.zero)


class MonthClock(Frozen):
    """Where we are in the budget month, for the month-end projection."""

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
    """The tunable: how far over a projection must run to count as trending.

    The budget-anchored blend (:func:`project_spend`) tames the early-month
    run-rate blowup at the source — a thin sample is pulled toward the
    category's own plan rather than extrapolated x31 — so no separate
    ``min_trend_day`` gate is needed to mute false alarms at month start.
    ALREADY_OVER is real arithmetic, not a projection, and alerts on any day.
    """

    trend_threshold: Money = Field(
        default_factory=lambda: Money.from_currency(25)
    )


DEFAULT_OVERSPEND_POLICY = OverspendPolicy()


class OverspendAssessment(Frozen):
    """The verdict for one category and the numbers behind it.

    ``available`` is the category's YNAB balance (rollover included) — the W7
    coverage need is sized against it, not against ``budgeted``, so a category
    sitting on carryover isn't asked to cover a phantom gap.
    """

    category: CategoryId
    name: str
    verdict: OverspendVerdict
    budgeted: Money
    spent: Money
    projected: Money
    available: Money


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
    bucketed into the right month and the projection's ``day_of_month`` is the
    household's, not UTC's (which is hours ahead).
    """
    local = now.astimezone(HOUSEHOLD_TZ)
    clock = MonthClock(
        day_of_month=local.day,
        days_in_month=calendar.monthrange(local.year, local.month)[1],
    )
    return local.strftime("%Y-%m"), clock


def project_spend(category: CategorySpend, clock: MonthClock) -> Money:
    """Project month-end spend with a budget-anchored blend (SPEC §7). Pure.

    A thin early-month sample makes a raw run-rate explode — a one-off lump on
    day 2 extrapolates to x15 its size — so the projection blends the burn rate
    with the category's own plan, shifting trust from plan to burn as the month
    fills in. With ``w = day_of_month / days_in_month``::

        run_rate  = spent / day_of_month * days_in_month   # the burn view
        anchor    = max(budgeted, spent)                   # the plan view
        projected = w * run_rate + (1 - w) * anchor + scheduled

    Early (``w → 0``) it trusts the plan, late (``w → 1``) the burn. Floored at
    ``spent + scheduled`` — money already gone can't be projected away. The
    blend is exact in milliunits, and stays monotonic day-to-day for a fixed
    spend, so a quiet category never churns a fresh alert. ``scheduled`` is the
    category's known future outflow this month (``scheduled_remaining``), added
    at full size — a $1,200 rent due on the 28th lands once, not as a daily
    rate.
    """
    spent = spent_magnitude(category)
    scheduled = category.scheduled_remaining
    day = clock.day_of_month
    month_len = clock.days_in_month
    run_rate = spent.milliunits * month_len // day
    anchor = max(category.budgeted.milliunits, spent.milliunits)
    blend = (day * run_rate + (month_len - day) * anchor) // month_len
    projected = Money.from_milliunits(blend) + scheduled
    floor = spent + scheduled
    return projected if projected > floor else floor


def assess(
    category: CategorySpend,
    clock: MonthClock,
    *,
    policy: OverspendPolicy = DEFAULT_OVERSPEND_POLICY,
) -> OverspendAssessment:
    """Rank a category against its *available* funds for the month (§7). Pure.

    "Over" is measured against YNAB ``balance`` (available = budgeted +
    carryover + activity), not against ``budgeted``: a category funded by
    rollover can spend past its ``budgeted`` and still be fine, exactly as YNAB
    only marks it overspent when ``balance`` goes negative. ALREADY_OVER is a
    negative balance today; TRENDING_OVER is the projected *remaining* spend
    driving available negative by more than the threshold; both bake rollover
    in. With zero carryover (``balance == budgeted + activity``) this reduces to
    the old ``spent``/``projected`` vs ``budgeted`` comparison.
    """
    spent = spent_magnitude(category)
    projected = project_spend(category, clock)
    available = category.balance
    projected_remaining = projected - spent

    if available < Money.zero():
        verdict = OverspendVerdict.ALREADY_OVER
    elif projected_remaining - available > policy.trend_threshold:
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
        available=available,
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
