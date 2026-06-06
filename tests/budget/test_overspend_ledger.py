"""Pure folds for the overspend-alert dedup ledger (W6, SPEC §7).

The dedupe memory is one entry per category, stamped with its budget period:
``prior`` reads the last alert back *only within the same period* (so a new
month resets), and ``record`` upserts so the tail stays one-per-category.
"""

from __future__ import annotations

from ynab_agent.budget.ledger import OverspendLedgerState, prior, record
from ynab_agent.budget.overspend import OverspendVerdict, PriorAlert
from ynab_agent.domain.money import Money


def _alert(
    projected: str = "500",
    verdict: OverspendVerdict = OverspendVerdict.TRENDING_OVER,
) -> PriorAlert:
    return PriorAlert(verdict=verdict, projected=Money.from_currency(projected))


def test_empty_ledger_has_no_prior() -> None:
    assert prior(OverspendLedgerState(), "dining", "2026-06") is None


def test_record_then_prior_same_period_returns_alert() -> None:
    state = record(OverspendLedgerState(), "dining", "2026-06", _alert())
    assert prior(state, "dining", "2026-06") == _alert()


def test_prior_resets_across_period_boundary() -> None:
    state = record(OverspendLedgerState(), "dining", "2026-06", _alert())
    assert prior(state, "dining", "2026-07") is None


def test_prior_is_per_category() -> None:
    state = record(OverspendLedgerState(), "dining", "2026-06", _alert())
    assert prior(state, "gas", "2026-06") is None


def test_record_keeps_one_entry_per_category() -> None:
    state = record(OverspendLedgerState(), "dining", "2026-06", _alert("500"))
    state = record(state, "dining", "2026-06", _alert("560"))
    assert len(state.entries) == 1
    got = prior(state, "dining", "2026-06")
    assert got is not None
    assert got.projected == Money.from_currency("560")


def test_record_accumulates_distinct_categories() -> None:
    state = record(OverspendLedgerState(), "dining", "2026-06", _alert())
    state = record(state, "gas", "2026-06", _alert("300"))
    assert len(state.entries) == 2
    assert prior(state, "dining", "2026-06") is not None
    assert prior(state, "gas", "2026-06") is not None
