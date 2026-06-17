"""W7 · the budget balancer — pure coverage planning (SPEC §8).

A reallocation of ``budgeted`` within YNAB to cover overspent/underfunded
categories: reversible, no money leaves an account. The proposed moves go
through the same propose-then-confirm + floor/gate spine as categorization
(SPEC §8); applying them is the workflow's job.

Two ways to arrive at the moves live here, both pure:

- :func:`plan_coverage` is the deterministic greedy planner — drain sources in
  priority order (Ready-to-Assign → buffer → over-funded). It is the safety-net
  fallback (:func:`fallback_option`) when the model proposes nothing usable.
- The model (``agentic.balance``) proposes several :class:`BalanceOption` s,
  each a set of moves with a human-facing rationale. :func:`validate_option`
  and :func:`feasible_options` are the deterministic guard over those proposals:
  the model invents no money and breaches no ceiling, no matter what it returns.

Money is abstract here: a "source" and a "destination" are just category-like
buckets with an available amount, so Ready-to-Assign is modelled as one source.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money
from ynab_agent.policy.floor import CAUTIOUS_FLOOR, FloorPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ynab_agent.budget.overspend import CategorySpend, OverspendAssessment


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
    needs: Iterable[Need],
    sources: Iterable[Source],
    policy: FloorPolicy = CAUTIOUS_FLOOR,
) -> CoveragePlan:
    """Greedily cover each need from sources in priority order (SPEC §8). Pure.

    Needs are covered in the order given; for each, sources are drained
    cheapest-priority first, each move capped at the floor's per-move ceiling —
    the fallback must never offer a plan the agent's own floor would refuse to
    apply. A need only partially covered is reported in ``uncovered`` with its
    remaining shortfall — the planner invents no money.
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
            take = _take(_take(available, shortfall), policy.per_move_ceiling)
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


class BalanceOption(Frozen):
    """One way to cover a need: moves plus a human-facing rationale (SPEC §8).

    The model proposes several of these; the owner picks one in plain English.
    ``label`` is a handle ("Pull from buffers"), ``rationale`` explains the
    tradeoff, and every move funds the same needy ``destination``.
    """

    label: str
    moves: tuple[BudgetMove, ...]
    rationale: str

    @property
    def total(self) -> Money:
        """The amount this option moves into the destination, summed. Pure."""
        total = Money.zero()
        for move in self.moves:
            total = total + move.amount
        return total


class OptionRejection(StrEnum):
    """Why a proposed option can't be applied as-is — a deterministic veto."""

    EMPTY = "empty"
    WRONG_DESTINATION = "wrong_destination"
    OVER_CEILING = "over_ceiling"
    INSUFFICIENT_SOURCE = "insufficient_source"
    DOES_NOT_COVER = "does_not_cover"


def validate_option(
    option: BalanceOption,
    need: Need,
    available: Mapping[CategoryId, Money],
    policy: FloorPolicy = CAUTIOUS_FLOOR,
) -> OptionRejection | None:
    """Why ``option`` can't cover ``need``, or ``None`` if it can. Pure.

    The deterministic guard over the model's proposals (SPEC §8, §0.6): every
    move must fund the needy category with a positive amount within the per-move
    ceiling, the sources must actually hold what the moves pull (summed across
    moves from the same source), and the moves together must meet the shortfall.
    ``available`` is the real, current funds per source category — the source of
    truth the model's numbers are checked against.
    """
    if not option.moves:
        return OptionRejection.EMPTY
    zero = Money.zero()
    pulled: dict[CategoryId, Money] = {}
    covered = zero
    for move in option.moves:
        if move.amount <= zero:
            return OptionRejection.EMPTY
        if move.destination != need.category:
            return OptionRejection.WRONG_DESTINATION
        if abs(move.amount) > policy.per_move_ceiling:
            return OptionRejection.OVER_CEILING
        pulled[move.source] = pulled.get(move.source, zero) + move.amount
        covered = covered + move.amount
    for source, amount in pulled.items():
        if amount > available.get(source, zero):
            return OptionRejection.INSUFFICIENT_SOURCE
    if covered < need.shortfall:
        return OptionRejection.DOES_NOT_COVER
    return None


def feasible_options(
    options: Iterable[BalanceOption],
    need: Need,
    sources: Iterable[Source],
    policy: FloorPolicy = CAUTIOUS_FLOOR,
) -> tuple[BalanceOption, ...]:
    """The options that pass :func:`validate_option`, order preserved. Pure."""
    available = {source.category: source.available for source in sources}
    return tuple(
        option
        for option in options
        if validate_option(option, need, available, policy) is None
    )


def fallback_option(
    need: Need, sources: Iterable[Source]
) -> BalanceOption | None:
    """The greedy plan as one labelled option, if it fully covers. Pure.

    The safety net when the model proposes nothing feasible (SPEC §8): reuse
    :func:`plan_coverage` so there is always an offer when the money exists.
    ``None`` when even the greedy plan can't cover the need.
    """
    plan = plan_coverage([need], list(sources))
    if not plan.fully_covered or not plan.moves:
        return None
    return BalanceOption(
        label="Automatic",
        moves=plan.moves,
        rationale=(
            "Pull from Ready to Assign first, then buffers, then over-funded "
            "categories, in that order."
        ),
    )


class ApplyMoves(Frozen):
    """The owner approved a concrete set of moves to apply (SPEC §8)."""

    kind: Literal["apply"] = "apply"
    moves: tuple[BudgetMove, ...]


class DeclineBalance(Frozen):
    """The owner declined the offer — make no moves."""

    kind: Literal["decline"] = "decline"


class ClarifyBalance(Frozen):
    """The reply was unclear — ask one follow-up before doing anything."""

    kind: Literal["clarify"] = "clarify"
    question: str


# What an owner's free-text reply to a balance offer resolves to: apply a plan,
# decline, or ask again. Mirrors ``ReplyOutcome`` — the deterministic seam the
# workflow branches on, so it never depends on the agent's own schema.
BalanceOutcome = Annotated[
    ApplyMoves | DeclineBalance | ClarifyBalance,
    Field(discriminator="kind"),
]


# Ready-to-Assign is modelled as a source but is NOT a real, patchable category:
# raising a category's budget lowers RTA on its own. So a move *from* this id
# only raises the destination — there is no source write. The "@" prefix cannot
# collide with a real YNAB category id (those are UUIDs).
READY_TO_ASSIGN_SOURCE = CategoryId("@ready-to-assign")


def need_from_assessment(assessment: OverspendAssessment) -> Need:
    """The coverage need from an overspend assessment (SPEC §7 → §8). Pure.

    Sized to keep *available* from going negative: the projected remaining spend
    (``projected - spent``) less the funds on hand (``available``, rollover
    included), floored at zero. Measuring against available rather than budgeted
    means a category sitting on carryover is never asked to cover a phantom gap.
    """
    projected_remaining = assessment.projected - assessment.spent
    shortfall = projected_remaining - assessment.available
    if shortfall < Money.zero():
        shortfall = Money.zero()
    return Need(category=assessment.category, shortfall=shortfall)


def sources_from_spends(
    spends: Iterable[CategorySpend],
    ready_to_assign: Money,
    *,
    exclude: CategoryId,
) -> tuple[Source, ...]:
    """The buckets the balancer may pull from (SPEC §8). Pure.

    Every category with a positive available balance, minus the needy category
    itself, plus Ready-to-Assign when positive, which leads; every real
    category is treated as over-funded — v1 has no designated buffer.
    """
    zero = Money.zero()
    sources: list[Source] = []
    if ready_to_assign > zero:
        sources.append(
            Source(
                category=READY_TO_ASSIGN_SOURCE,
                available=ready_to_assign,
                priority=SourcePriority.READY_TO_ASSIGN,
            )
        )
    sources.extend(
        Source(
            category=spend.category,
            available=spend.balance,
            priority=SourcePriority.OVERFUNDED,
        )
        for spend in spends
        if spend.category != exclude and spend.balance > zero
    )
    return tuple(sources)


def _pulled_by_source(
    moves: Iterable[BudgetMove],
) -> dict[CategoryId, Money]:
    """Total pulled from each source across the moves. Pure."""
    zero = Money.zero()
    pulled: dict[CategoryId, Money] = {}
    for move in moves:
        pulled[move.source] = pulled.get(move.source, zero) + move.amount
    return pulled


def check_moves(
    moves: tuple[BudgetMove, ...],
    available: Mapping[CategoryId, Money],
    policy: FloorPolicy = CAUTIOUS_FLOOR,
) -> OptionRejection | None:
    """Why these owner-approved moves can't be applied, or ``None``. Pure.

    The apply-time guard: positive amounts within the per-move ceiling, and no
    source pulled past its available funds. Unlike :func:`validate_option` it
    does NOT require covering the shortfall — the owner may choose to cover only
    part ("only $50").
    """
    if not moves:
        return OptionRejection.EMPTY
    zero = Money.zero()
    for move in moves:
        if move.amount <= zero:
            return OptionRejection.EMPTY
        if abs(move.amount) > policy.per_move_ceiling:
            return OptionRejection.OVER_CEILING
    for source, amount in _pulled_by_source(moves).items():
        if amount > available.get(source, zero):
            return OptionRejection.INSUFFICIENT_SOURCE
    return None


def move_targets(
    moves: tuple[BudgetMove, ...],
    current_budgeted: Mapping[CategoryId, Money],
) -> dict[CategoryId, Money]:
    """The absolute ``budgeted`` each real category should be set to. Pure.

    A move raises its destination and lowers its source; a move *from*
    Ready-to-Assign only raises the destination (YNAB lowers RTA on its own), so
    the sentinel source is never written. Categories with no change drop out.
    A retry re-derives the same targets, so the absolute writes stay idempotent.
    """
    zero = Money.zero()
    delta: dict[CategoryId, Money] = {}
    for move in moves:
        delta[move.destination] = (
            delta.get(move.destination, zero) + move.amount
        )
        if move.source != READY_TO_ASSIGN_SOURCE:
            delta[move.source] = delta.get(move.source, zero) - move.amount
    return {
        category: current_budgeted[category] + change
        for category, change in delta.items()
        if not change.is_zero
    }
