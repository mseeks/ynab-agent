"""Value types for the W6 overspend-monitor workflow."""

from __future__ import annotations

from ynab_agent.budget.overspend import MonthClock
from ynab_agent.domain.base import Frozen


class MonitorParams(Frozen):
    """One monitor pass. ``clock`` is derived from the workflow clock if unset.

    A fixed ``clock`` makes a run deterministic for tests and lets a caller
    monitor an explicit day; production passes ``None`` and the workflow derives
    the month position via the ``current_period`` activity (household time).
    """

    clock: MonthClock | None = None


class PeriodClock(Frozen):
    """The budget period and month position, derived in household time (§13).

    Produced by the ``current_period`` activity — the timezone conversion runs
    outside the workflow sandbox, and the recorded result keeps replay
    deterministic.
    """

    period: str
    clock: MonthClock


class MonitorResult(Frozen):
    """The outcome of a monitor pass (for observability)."""

    categories: int
    alerts: int
    alerted: tuple[str, ...] = ()
