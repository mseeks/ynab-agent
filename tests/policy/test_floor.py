"""Tests for the hard floor (SPEC §0.6 Layer 1)."""

from __future__ import annotations

from ynab_agent.domain.money import Money
from ynab_agent.policy.floor import (
    AutoActionCounters,
    FloorVerdict,
    check_budget_move_floor,
    check_floor,
)


def test_unreadable_amount_forces_human() -> None:
    assert check_floor(None, AutoActionCounters()) is FloorVerdict.FORCE_HUMAN


def test_over_ceiling_forces_human() -> None:
    # Cautious ceiling is $75; an $80 outflow exceeds it by magnitude.
    verdict = check_floor(Money.from_currency("-80"), AutoActionCounters())
    assert verdict is FloorVerdict.FORCE_HUMAN


def test_within_ceiling_allows() -> None:
    verdict = check_floor(Money.from_currency("-40"), AutoActionCounters())
    assert verdict is FloorVerdict.ALLOW


def test_run_cap_trips_breaker() -> None:
    counters = AutoActionCounters(this_run=8)
    verdict = check_floor(Money.from_currency("-5"), counters)
    assert verdict is FloorVerdict.TRIP_BREAKER


def test_day_cap_trips_breaker() -> None:
    counters = AutoActionCounters(today=20)
    verdict = check_floor(Money.from_currency("-5"), counters)
    assert verdict is FloorVerdict.TRIP_BREAKER


def test_ceiling_is_checked_before_the_breaker() -> None:
    # Over ceiling AND breaker exhausted → FORCE_HUMAN (ceiling wins).
    counters = AutoActionCounters(this_run=8)
    verdict = check_floor(Money.from_currency("-200"), counters)
    assert verdict is FloorVerdict.FORCE_HUMAN


def test_budget_move_within_ceiling_allows() -> None:
    # Cautious per-move ceiling is $500; a $300 move is fine.
    verdict = check_budget_move_floor(
        Money.from_currency("300"), AutoActionCounters()
    )
    assert verdict is FloorVerdict.ALLOW


def test_budget_move_over_ceiling_forces_human() -> None:
    # Over the $500 per-move ceiling drops to a human even when confirmed.
    verdict = check_budget_move_floor(
        Money.from_currency("600"), AutoActionCounters()
    )
    assert verdict is FloorVerdict.FORCE_HUMAN


def test_budget_move_daily_cap_trips_breaker() -> None:
    counters = AutoActionCounters(today=10)
    verdict = check_budget_move_floor(Money.from_currency("50"), counters)
    assert verdict is FloorVerdict.TRIP_BREAKER


def test_budget_move_ceiling_checked_before_the_breaker() -> None:
    counters = AutoActionCounters(today=10)
    verdict = check_budget_move_floor(Money.from_currency("600"), counters)
    assert verdict is FloorVerdict.FORCE_HUMAN
