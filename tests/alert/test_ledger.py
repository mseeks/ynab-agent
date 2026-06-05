"""Tests for the alert-dedup ledger's pure folds (SPEC §13).

The two anti-bombard rules: a per-key cooldown (the same failure re-firing every
poll tick alerts once a day, not hourly) and a global rate cap (a systemic break
across many distinct keys alerts a few times, not N).
"""

from __future__ import annotations

import datetime

from hypothesis import given
from hypothesis import strategies as st

from ynab_agent.alert.ledger import (
    DEFAULT_COOLDOWN,
    DEFAULT_RATE_CAP,
    DEFAULT_RATE_WINDOW,
    LedgerState,
    record,
    should_notify,
)

_EPOCH = datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.UTC)


def test_empty_ledger_always_notifies() -> None:
    assert should_notify(LedgerState(), "txn-1", _EPOCH) is True


def test_same_key_is_suppressed_within_cooldown() -> None:
    state = record(LedgerState(), "txn-1", _EPOCH)
    # An hour later (the poll re-fire) the same key stays quiet.
    later = _EPOCH + datetime.timedelta(hours=1)
    assert should_notify(state, "txn-1", later) is False


def test_same_key_notifies_again_after_cooldown() -> None:
    state = record(LedgerState(), "txn-1", _EPOCH)
    after = _EPOCH + DEFAULT_COOLDOWN + datetime.timedelta(seconds=1)
    assert should_notify(state, "txn-1", after) is True


def test_a_different_key_is_not_blocked_by_the_cooldown() -> None:
    state = record(LedgerState(), "txn-1", _EPOCH)
    assert should_notify(state, "txn-2", _EPOCH) is True


def test_rate_cap_blocks_distinct_keys_beyond_the_cap() -> None:
    # Fill the rolling window with the cap's worth of *distinct* keys.
    state = LedgerState()
    for i in range(DEFAULT_RATE_CAP):
        state = record(
            state, f"txn-{i}", _EPOCH + datetime.timedelta(minutes=i)
        )
    # A brand-new key now — under cooldown it would notify, but the global cap
    # is reached, so a systemic break goes quiet instead of flooding.
    when = _EPOCH + datetime.timedelta(minutes=DEFAULT_RATE_CAP)
    assert should_notify(state, "txn-new", when) is False


def test_rate_cap_recovers_after_the_window_rolls_off() -> None:
    state = LedgerState()
    for i in range(DEFAULT_RATE_CAP):
        state = record(state, f"txn-{i}", _EPOCH)
    after = _EPOCH + DEFAULT_RATE_WINDOW + datetime.timedelta(seconds=1)
    assert should_notify(state, "txn-new", after) is True


def test_record_prunes_entries_older_than_retention() -> None:
    state = record(LedgerState(), "old", _EPOCH)
    much_later = _EPOCH + DEFAULT_COOLDOWN + datetime.timedelta(hours=1)
    state = record(state, "new", much_later)
    # The stale 'old' entry is gone; only 'new' remains.
    assert [entry.key for entry in state.entries] == ["new"]


@given(
    keys=st.lists(st.text(min_size=1, max_size=4), min_size=1, max_size=20),
    minutes=st.lists(st.integers(min_value=0, max_value=10_000), max_size=20),
)
def test_recorded_key_is_quiet_until_cooldown(
    keys: list[str], minutes: list[int]
) -> None:
    """After recording a key, re-asking within the cooldown is always False."""
    state = LedgerState()
    for key, minute in zip(keys, minutes, strict=False):
        state = record(state, key, _EPOCH + datetime.timedelta(minutes=minute))
    # Record one more, then immediately re-check it within the cooldown.
    state = record(state, "probe", _EPOCH)
    within = _EPOCH + DEFAULT_COOLDOWN - datetime.timedelta(seconds=1)
    assert should_notify(state, "probe", within) is False
