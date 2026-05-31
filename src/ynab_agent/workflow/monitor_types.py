"""Value types for the W6 overspend-monitor workflow."""

from __future__ import annotations

from ynab_agent.budget.overspend import MonthClock
from ynab_agent.domain.base import Frozen


class MonitorParams(Frozen):
    """One monitor pass. ``clock`` is derived from the workflow clock if unset.

    A fixed ``clock`` makes a run deterministic for tests and lets a caller
    monitor an explicit day; production passes ``None`` and the workflow reads
    ``workflow.now()``.
    """

    clock: MonthClock | None = None


class MonitorResult(Frozen):
    """The outcome of a monitor pass (for observability)."""

    categories: int
    alerts: int
    alerted: tuple[str, ...] = ()
