"""Tests for the Money value object."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ynab_agent.domain.money import Money


def test_from_currency_scales_to_milliunits() -> None:
    assert Money.from_currency("4.50").milliunits == 4500
    assert Money.from_currency("-4.73").milliunits == -4730
    assert Money.from_currency(120).milliunits == 120_000


def test_from_currency_rounds_half_to_even() -> None:
    # 1.2345 -> 1234.5 milliunits -> nearest even is 1234.
    assert Money.from_currency("1.2345").milliunits == 1234
    # 1.2355 -> 1235.5 -> nearest even is 1236.
    assert Money.from_currency("1.2355").milliunits == 1236


def test_currency_amount_round_trips() -> None:
    assert Money.from_milliunits(4500).currency_amount == Decimal("4.5")


def test_arithmetic() -> None:
    a = Money.from_milliunits(4000)
    b = Money.from_milliunits(1500)
    assert (a + b).milliunits == 5500
    assert (a - b).milliunits == 2500
    assert (-a).milliunits == -4000
    assert abs(Money.from_milliunits(-7)).milliunits == 7


def test_comparisons() -> None:
    assert Money.from_milliunits(10) < Money.from_milliunits(20)
    assert Money.from_milliunits(20) >= Money.from_milliunits(20)
    assert Money.from_milliunits(5) <= Money.from_milliunits(5)
    assert Money.from_milliunits(30) > Money.from_milliunits(20)


def test_predicates() -> None:
    assert Money.zero().is_zero
    assert Money.from_milliunits(-1).is_outflow
    assert not Money.from_milliunits(1).is_outflow


def test_equality_and_hashing() -> None:
    assert Money.from_milliunits(100) == Money.from_milliunits(100)
    assert len({Money.from_milliunits(1), Money.from_milliunits(1)}) == 1


def test_str_formats_currency() -> None:
    assert str(Money.from_currency("4.50")) == "$4.50"


def test_is_frozen() -> None:
    m = Money.from_milliunits(1)
    field = "milliunits"  # a variable dodges B010 while staying mypy-clean
    with pytest.raises(ValidationError):
        setattr(m, field, 2)
