"""Tests for the W6 overspend monitor's pure projection/alerting (SPEC §7)."""

from __future__ import annotations

import datetime

import pytest

from ynab_agent.budget.overspend import (
    CategorySpend,
    MonthClock,
    OverspendVerdict,
    PriorAlert,
    assess,
    period_and_clock,
    project_spend,
    should_alert,
    spent_magnitude,
)
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money


def _category(
    *,
    budgeted: str,
    activity: str,
    balance: str | None = None,
    scheduled: str = "0",
) -> CategorySpend:
    # activity is YNAB-native: negative for spending. ``balance`` defaults to
    # zero-rollover (budgeted + activity); pass it to model carried-in funds.
    bud = Money.from_currency(budgeted)
    act = Money.from_currency(activity)
    return CategorySpend(
        category=CategoryId("dining"),
        name="Dining Out",
        budgeted=bud,
        activity=act,
        balance=Money.from_currency(balance)
        if balance is not None
        else bud + act,
        scheduled_remaining=Money.from_currency(scheduled),
    )


def test_period_and_clock_uses_household_timezone_at_a_month_boundary() -> None:
    # 03:00 UTC on Jun 1 is still 22:00 May 31 in US Central (CDT, UTC-5): the
    # budget month is May and the run-rate day is the 31st — not UTC's Jun 1.
    utc = datetime.datetime(2026, 6, 1, 3, 0, tzinfo=datetime.UTC)
    period, clock = period_and_clock(utc)
    assert period == "2026-05"
    assert clock.day_of_month == 31
    assert clock.days_in_month == 31


def test_period_and_clock_matches_the_local_day_midmonth() -> None:
    utc = datetime.datetime(
        2026, 6, 15, 18, 0, tzinfo=datetime.UTC
    )  # 13:00 CDT
    period, clock = period_and_clock(utc)
    assert period == "2026-06"
    assert clock.day_of_month == 15
    assert clock.days_in_month == 30


def test_spent_magnitude_flips_outflow_sign() -> None:
    assert spent_magnitude(
        _category(budgeted="400", activity="-210")
    ) == Money.from_currency("210")


def test_spent_magnitude_zero_when_net_inflow() -> None:
    assert spent_magnitude(_category(budgeted="400", activity="50")).is_zero


def test_blend_weights_burn_and_plan_at_mid_month() -> None:
    # $210 spent over 15 of 30 days. Burn view (run-rate) = $420, plan view
    # (anchor) = budgeted $400; at w = 1/2 the blend lands halfway: $410.
    cat = _category(budgeted="400", activity="-210")
    clock = MonthClock(day_of_month=15, days_in_month=30)
    assert project_spend(cat, clock) == Money.from_currency("410")


def test_day_two_lump_is_anchored_not_extrapolated() -> None:
    # A $300 one-off on a $300 budget, on day 2 of 31. The old bare run-rate
    # extrapolated this to ~$4,650 (x15) and fired; the blend anchors it near
    # the plan (~$581), an order of magnitude lower.
    cat = _category(budgeted="300", activity="-300")
    clock = MonthClock(day_of_month=2, days_in_month=31)
    projected = project_spend(cat, clock)
    assert Money.from_currency("300") < projected < Money.from_currency("700")


def test_sparse_category_projects_toward_budget() -> None:
    # Nothing spent yet: the blend leans on the plan, projecting toward budget
    # (not zero, not over) — so an untouched category never reads as trending.
    cat = _category(budgeted="400", activity="0")
    clock = MonthClock(day_of_month=5, days_in_month=30)
    assert project_spend(cat, clock) <= Money.from_currency("400")


def test_scheduled_outflows_add_to_projection() -> None:
    clock = MonthClock(day_of_month=10, days_in_month=30)
    base = project_spend(_category(budgeted="400", activity="-200"), clock)
    with_scheduled = project_spend(
        _category(budgeted="400", activity="-200", scheduled="50"), clock
    )
    # A scheduled outflow rides on top of the blend at full size, once.
    assert with_scheduled == base + Money.from_currency("50")


def test_big_scheduled_charge_raises_projection_by_its_full_size() -> None:
    # A $1,200 rent due late in the month lands once at full size — not as a
    # daily rate — on a category that has otherwise barely spent (SPEC §7).
    clock = MonthClock(day_of_month=27, days_in_month=30)
    base = project_spend(_category(budgeted="1500", activity="0"), clock)
    with_rent = project_spend(
        _category(budgeted="1500", activity="0", scheduled="1200"), clock
    )
    assert with_rent == base + Money.from_currency("1200")


def test_already_over_when_available_is_negative() -> None:
    # Spent $420 of $400 with no rollover → balance -$20 → already over (YNAB's
    # own definition: a category is overspent when available goes negative).
    out = assess(
        _category(budgeted="400", activity="-420"),
        MonthClock(day_of_month=24, days_in_month=30),
    )
    assert out.verdict is OverspendVerdict.ALREADY_OVER


def test_rollover_funded_category_does_not_alert() -> None:
    # budgeted $0 but $200 carried in, spends $50 → available $150. The old
    # spent-vs-budgeted test fired a phantom alert; measured against available
    # (rollover baked in), it's fine.
    cat = _category(budgeted="0", activity="-50", balance="150")
    out = assess(cat, MonthClock(day_of_month=15, days_in_month=30))
    assert out.verdict is OverspendVerdict.OK
    assert should_alert(out, None) is False


def test_scheduled_charge_can_drive_a_category_to_trend() -> None:
    # $300 available, a $400 rent scheduled this month → the projection drives
    # available negative, so it trends over (the scheduled term is doing it).
    cat = _category(
        budgeted="400", activity="-100", balance="300", scheduled="400"
    )
    out = assess(cat, MonthClock(day_of_month=20, days_in_month=30))
    assert out.verdict is OverspendVerdict.TRENDING_OVER


def test_trending_over_when_projection_exceeds_threshold() -> None:
    # Halfway through, $250 of $400: burn $500 blended with plan $400 → $450,
    # over by $50 > $25.
    out = assess(
        _category(budgeted="400", activity="-250"),
        MonthClock(day_of_month=15, days_in_month=30),
    )
    assert out.verdict is OverspendVerdict.TRENDING_OVER


def test_steady_burn_still_trends_late_in_month() -> None:
    # On pace to finish meaningfully over: $320 of $400 by day 20 of 30. Late
    # in the month the burn dominates the blend (~$453), so it still trends.
    out = assess(
        _category(budgeted="400", activity="-320"),
        MonthClock(day_of_month=20, days_in_month=30),
    )
    assert out.verdict is OverspendVerdict.TRENDING_OVER


def test_ok_when_on_track() -> None:
    out = assess(
        _category(budgeted="400", activity="-180"),
        MonthClock(day_of_month=15, days_in_month=30),
    )
    assert out.verdict is OverspendVerdict.OK


def test_no_alert_when_ok() -> None:
    out = assess(
        _category(budgeted="400", activity="-100"),
        MonthClock(day_of_month=15, days_in_month=30),
    )
    assert should_alert(out, None) is False


def test_first_flag_of_period_alerts() -> None:
    out = assess(
        _category(budgeted="400", activity="-420"),
        MonthClock(day_of_month=24, days_in_month=30),
    )
    assert should_alert(out, None) is True


def test_repeat_flag_is_deduped_unless_worse() -> None:
    out = assess(
        _category(budgeted="400", activity="-250"),
        MonthClock(day_of_month=15, days_in_month=30),
    )
    prior = PriorAlert(verdict=out.verdict, projected=out.projected)
    # Same projection as last alert → no re-alert.
    assert should_alert(out, prior) is False


def test_escalation_to_already_over_re_alerts() -> None:
    out = assess(
        _category(budgeted="400", activity="-450"),
        MonthClock(day_of_month=28, days_in_month=30),
    )
    prior = PriorAlert(
        verdict=OverspendVerdict.TRENDING_OVER,
        projected=out.projected,
    )
    assert should_alert(out, prior) is True


def test_month_clock_rejects_day_past_month_end() -> None:
    with pytest.raises(ValueError, match="day_of_month"):
        MonthClock(day_of_month=31, days_in_month=30)


def test_small_early_charge_does_not_trend() -> None:
    # Day 1: $30 of $400. A bare run-rate extrapolates this to $930 (x31) and
    # would fire; the blend anchors a thin sample to the plan (~$417), so a
    # normal early charge stays OK with no min-trend-day band-aid.
    out = assess(
        _category(budgeted="400", activity="-30"),
        MonthClock(day_of_month=1, days_in_month=31),
    )
    assert out.verdict is OverspendVerdict.OK


def test_already_over_still_alerts_on_day_one() -> None:
    # Real arithmetic, not a projection: a genuinely over-budget category
    # alerts on any day.
    out = assess(
        _category(budgeted="400", activity="-420"),
        MonthClock(day_of_month=1, days_in_month=31),
    )
    assert out.verdict is OverspendVerdict.ALREADY_OVER


def test_genuine_over_pace_trends_even_early() -> None:
    # $150 of $400 by day 5 of 30 is a real 3x over-pace, not a single lump:
    # the burn ($900) pulls the blend well over budget (~$483), so it trends
    # on its own merits — no day gate required.
    out = assess(
        _category(budgeted="400", activity="-150"),
        MonthClock(day_of_month=5, days_in_month=30),
    )
    assert out.verdict is OverspendVerdict.TRENDING_OVER


def test_early_lump_alerts_once_then_never_churns() -> None:
    # The accepted residual of the linear blend: a category that spends its
    # whole budget early gets ONE alert, then the projection only falls (it is
    # monotonic for a fixed spend), so should_alert never re-fires — the
    # day-to-day churn that #43 set out to kill cannot recur.
    cat = _category(budgeted="300", activity="-300")
    early = assess(cat, MonthClock(day_of_month=2, days_in_month=31))
    assert early.verdict is OverspendVerdict.TRENDING_OVER
    assert should_alert(early, None) is True  # first flag of the period
    first = PriorAlert(verdict=early.verdict, projected=early.projected)
    later = assess(cat, MonthClock(day_of_month=12, days_in_month=31))
    assert later.projected < early.projected  # only fell, no new spend
    assert should_alert(later, first) is False  # no re-alert, no churn
