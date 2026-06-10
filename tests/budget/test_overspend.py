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


def _category(*, budgeted: str, activity: str) -> CategorySpend:
    # activity is YNAB-native: negative for spending.
    return CategorySpend(
        category=CategoryId("dining"),
        name="Dining Out",
        budgeted=Money.from_currency(budgeted),
        activity=Money.from_currency(activity),
        balance=Money.from_currency(budgeted) + Money.from_currency(activity),
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


def test_run_rate_projection_doubles_at_mid_month() -> None:
    # $210 spent over 15 of 30 days → projects to ~$420.
    cat = _category(budgeted="400", activity="-210")
    clock = MonthClock(day_of_month=15, days_in_month=30)
    assert project_spend(cat, clock, Money.zero()) == Money.from_currency("420")


def test_scheduled_outflows_add_to_projection() -> None:
    cat = _category(budgeted="400", activity="-200")
    clock = MonthClock(day_of_month=10, days_in_month=30)
    # run-rate 200*30//10 = 600, plus a $50 scheduled charge = 650.
    projected = project_spend(cat, clock, Money.from_currency("50"))
    assert projected == Money.from_currency("650")


def test_already_over_when_spent_exceeds_budget() -> None:
    out = assess(
        _category(budgeted="400", activity="-420"),
        MonthClock(day_of_month=24, days_in_month=30),
    )
    assert out.verdict is OverspendVerdict.ALREADY_OVER


def test_trending_over_when_projection_exceeds_threshold() -> None:
    # Halfway through, $250 of $400 → projects ~$500, over by $100 > $25.
    out = assess(
        _category(budgeted="400", activity="-250"),
        MonthClock(day_of_month=15, days_in_month=30),
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


def test_trending_is_suppressed_in_the_first_days_of_the_month() -> None:
    # Day 1: $30 of $400 projects to $930 (x31) — a normal charge, not a
    # blowout. The run-rate is meaningless this early; no trending alarm.
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


def test_trending_fires_once_the_month_is_credible() -> None:
    out = assess(
        _category(budgeted="400", activity="-150"),
        MonthClock(day_of_month=5, days_in_month=30),
    )
    assert out.verdict is OverspendVerdict.TRENDING_OVER
