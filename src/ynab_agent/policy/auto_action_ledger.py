"""The auto-action ledger's pure state and folds — the circuit-breaker memory.

The hard floor's per-run / per-day circuit breaker (SPEC §0.6 Layer 1) caps how
many auto-actions the agent may take, bounding a runaway poller or a mis-blessed
rule regardless of the model. That cap can only bind if the counts are *real*:
this module is the durable memory those counts derive from — an append-only tail
of recent auto-actions, pruned to a day, that :func:`counters` reads into the
:class:`~ynab_agent.policy.floor.AutoActionCounters` the floor checks. Without
it the counters are always zero and the breaker can never trip.

A "run" has no shared identity across the per-transaction workflows, so it is
approximated as a short rolling window — about one poll cycle's burst (the
poller defaults to hourly). "Today" is the trailing 24 h. Both are rate limits
the floor compares to its caps. Entries are keyed by ``ynab_id`` and
deduplicated, so a retried record (or re-enriched transaction) counts once.
"""

from __future__ import annotations

import datetime

from ynab_agent.domain.base import Frozen
from ynab_agent.policy.floor import AutoActionCounters

# "This run" has no shared id across per-txn workflows, so approximate it as a
# short rolling window — about one poll cycle (the poller defaults to hourly).
RUN_WINDOW = datetime.timedelta(hours=1)
# "Today" — the trailing day the per-day cap bounds, and the prune horizon, so
# the carried tail never grows without limit.
DAY_WINDOW = datetime.timedelta(hours=24)


class AutoActionEntry(Frozen):
    """One auto-action that landed: its transaction id and when it landed."""

    ynab_id: str
    at: datetime.datetime


class AutoActionLedgerState(Frozen):
    """The pruned tail of recent auto-actions — the whole breaker memory.

    Held as Temporal workflow state and carried across continue-as-new, so it is
    a frozen value like the rest of the domain.
    """

    entries: tuple[AutoActionEntry, ...] = ()


def record(
    state: AutoActionLedgerState,
    ynab_id: str,
    now: datetime.datetime,
    *,
    retention: datetime.timedelta = DAY_WINDOW,
) -> AutoActionLedgerState:
    """Record an auto-action for ``ynab_id``; dedup it and prune. Pure.

    Any existing entry for the same ``ynab_id`` is dropped before appending,
    so a retried record or a re-enriched transaction counts exactly once.
    Entries older than ``retention`` are pruned, so the carried tail stays
    bounded.
    """
    kept = tuple(
        entry
        for entry in state.entries
        if entry.ynab_id != ynab_id and now - entry.at < retention
    )
    return AutoActionLedgerState(
        entries=(*kept, AutoActionEntry(ynab_id=ynab_id, at=now))
    )


def counters(
    state: AutoActionLedgerState,
    now: datetime.datetime,
    *,
    run_window: datetime.timedelta = RUN_WINDOW,
    day_window: datetime.timedelta = DAY_WINDOW,
) -> AutoActionCounters:
    """Project the tail into the floor's counters at ``now``. Pure.

    ``this_run`` is the count within ``run_window`` (the recent burst);
    ``today`` is the count within ``day_window``. The floor (``check_floor``)
    compares these to its per-run / per-day caps.
    """
    this_run = sum(1 for entry in state.entries if now - entry.at < run_window)
    today = sum(1 for entry in state.entries if now - entry.at < day_window)
    return AutoActionCounters(this_run=this_run, today=today)
