"""W7 · the budget balancer — pure coverage planning (SPEC §8).

A reallocation of ``budgeted`` within YNAB to cover overspent/underfunded
categories: reversible, no money leaves an account. This module is the pure
planner — given the needs and the available sources (in priority order:
Ready-to-Assign → buffer → over-funded discretionary), :func:`plan_coverage`
greedily proposes the moves. The proposed moves then go through the same
propose-then-confirm + floor/gate spine as categorization (SPEC §8); applying
them is the workflow's job.

Money is abstract here: a "source" and a "destination" are just category-like
buckets with an available amount, so Ready-to-Assign is modelled as one source.
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money

if TYPE_CHECKING:
    from collections.abc import Iterable


class SourcePriority(IntEnum):
    """Where to pull from first; lower is preferred (SPEC §8 step 2)."""

    READY_TO_ASSIGN = 0
    BUFFER = 1
    OVERFUNDED = 2


class Need(Frozen):
    """A category short by ``shortfall`` (a positive amount to cover)."""

    category: CategoryId
    shortfall: Money

    @property
    def is_met(self) -> bool:
        """Whether nothing more is owed (shortfall is zero or negative)."""
        return self.shortfall <= Money.zero()


class Source(Frozen):
    """A bucket money can be pulled from, with its priority class."""

    category: CategoryId
    available: Money
    priority: SourcePriority


class BudgetMove(Frozen):
    """A proposed reallocation of ``budgeted`` from one bucket to another."""

    source: CategoryId
    destination: CategoryId
    amount: Money


class CoveragePlan(Frozen):
    """The proposed moves and any needs that could not be fully covered."""

    moves: tuple[BudgetMove, ...]
    uncovered: tuple[Need, ...]

    @property
    def fully_covered(self) -> bool:
        """Whether every need was met from available sources."""
        return not self.uncovered


def _take(available: Money, wanted: Money) -> Money:
    """The amount drawable: the smaller of what is available and wanted."""
    return available if available < wanted else wanted


def plan_coverage(
    needs: Iterable[Need], sources: Iterable[Source]
) -> CoveragePlan:
    """Greedily cover each need from sources in priority order (SPEC §8). Pure.

    Needs are covered in the order given; for each, sources are drained
    cheapest-priority first. A need only partially covered is reported in
    ``uncovered`` with its remaining shortfall — the planner invents no money.
    """
    remaining = {source.category: source.available for source in sources}
    ordered = sorted(sources, key=lambda s: (s.priority, str(s.category)))

    moves: list[BudgetMove] = []
    uncovered: list[Need] = []
    zero = Money.zero()
    for need in needs:
        shortfall = need.shortfall
        for source in ordered:
            if shortfall <= zero:
                break
            available = remaining[source.category]
            if available <= zero:
                continue
            take = _take(available, shortfall)
            moves.append(
                BudgetMove(
                    source=source.category,
                    destination=need.category,
                    amount=take,
                )
            )
            remaining[source.category] = available - take
            shortfall = shortfall - take
        if shortfall > zero:
            uncovered.append(Need(category=need.category, shortfall=shortfall))
    return CoveragePlan(moves=tuple(moves), uncovered=tuple(uncovered))
