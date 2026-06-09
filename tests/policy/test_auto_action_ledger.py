"""Tests for the auto-action circuit-breaker ledger folds (SPEC §0.6)."""

from __future__ import annotations

import datetime

from ynab_agent.policy.auto_action_ledger import (
    AutoActionLedgerState,
    counters,
    record,
)

_NOW = datetime.datetime(2026, 6, 8, 12, 0, tzinfo=datetime.UTC)


def _ago(**kw: float) -> datetime.datetime:
    return _NOW - datetime.timedelta(**kw)


def test_record_appends_an_entry() -> None:
    state = record(AutoActionLedgerState(), "t1", _NOW)
    assert [entry.ynab_id for entry in state.entries] == ["t1"]


def test_record_dedups_the_same_transaction() -> None:
    # A retried record or a re-enriched txn must count once, not twice.
    state = record(AutoActionLedgerState(), "t1", _ago(minutes=30))
    state = record(state, "t1", _NOW)
    assert len(state.entries) == 1
    assert state.entries[0].at == _NOW  # the latest timestamp wins


def test_record_prunes_entries_older_than_retention() -> None:
    state = record(AutoActionLedgerState(), "old", _ago(hours=25))
    state = record(state, "t2", _NOW)
    assert [entry.ynab_id for entry in state.entries] == ["t2"]


def test_counters_split_run_and_day_windows() -> None:
    state = AutoActionLedgerState()
    state = record(state, "t1", _ago(minutes=10))  # within run + day
    state = record(state, "t2", _ago(minutes=50))  # within run + day
    state = record(state, "t3", _ago(hours=5))  # within day only (run = 1 h)
    result = counters(state, _NOW)
    assert result.this_run == 2
    assert result.today == 3


def test_counters_empty_ledger_is_zero() -> None:
    result = counters(AutoActionLedgerState(), _NOW)
    assert result.this_run == 0
    assert result.today == 0
