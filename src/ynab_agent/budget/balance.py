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

from ynab_agent.budget.overspend import project_spend, spent_magnitude
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money
from ynab_agent.policy.floor import CAUTIOUS_FLOOR, FloorPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ynab_agent.budget.overspend import (
        CategorySpend,
        MonthClock,
        OverspendAssessment,
    )


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
    """A bucket money can be pulled from, with its priority class.

    ``available`` is the raw balance (rollover included). ``slack`` is what the
    donor can actually spare *after* protecting its own projected spend — the
    drawable cap. ``projection`` is its own projected month-end spend, carried
    for the model and the offer's after-state. When ``slack`` is unset it falls
    back to ``available`` (the pre-slack behavior), so the greedy planner and
    the guard need no special-casing.
    """

    category: CategoryId
    available: Money
    priority: SourcePriority
    slack: Money | None = None
    projection: Money = Field(default_factory=Money.zero)

    @property
    def drawable(self) -> Money:
        """What may be pulled: the protected slack, or the balance if unset."""
        return self.available if self.slack is None else self.slack


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
    cheapest-priority first, ranked by slack within a priority class, each move
    capped at the floor's per-move ceiling — the planner must never offer a plan
    the agent's own floor would refuse to apply. A rich source is drawn in
    several capped moves until it reaches its ``drawable`` slack (never below
    its own protected spend) or the need is met, so one donor can fully cover a
    need larger than the ceiling. A need still short after every source is
    reported in ``uncovered`` — the planner invents no money.
    """
    remaining = {source.category: source.drawable for source in sources}
    ordered = sorted(
        sources, key=lambda s: (s.priority, -s.drawable.milliunits)
    )

    moves: list[BudgetMove] = []
    uncovered: list[Need] = []
    zero = Money.zero()
    for need in needs:
        shortfall = need.shortfall
        for source in ordered:
            if shortfall <= zero:
                break
            # Draw from this source in per-move-ceiling chunks until it is
            # drained to its slack or the need is met — so a single rich donor
            # can fully cover a need larger than the ceiling, in several moves.
            while shortfall > zero and remaining[source.category] > zero:
                available = remaining[source.category]
                take = _take(
                    _take(available, shortfall), policy.per_move_ceiling
                )
                if take <= zero:
                    break
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


class SourceView(Frozen):
    """A donor's display facts: its name and the slack it has to spare (§8).

    Carried alongside the options so the offer can render real numbers — the
    source name, and what each move leaves it ("~$430 still to spare").
    """

    category: CategoryId
    name: str
    slack: Money


class BalanceOffer(Frozen):
    """The coverage options plus the donor facts needed to render them (§8).

    ``options`` are the validated ways to cover the shortfall; ``sources`` are
    the donor views (name + slack) the renderer looks up to show amounts, donor
    names, and after-state. Empty ``options`` means nothing safe can cover it.
    """

    options: tuple[BalanceOption, ...]
    sources: tuple[SourceView, ...]


class CoverageLine(Frozen):
    """One move of a coordinated plan, named for rendering (SPEC §8, #46)."""

    amount: Money
    destination: str  # the funded category's name
    source: str  # the donor's name


class CoordinatedOffer(Frozen):
    """One coordinated plan for a whole pass + the facts to render it (#46).

    ``moves`` is the single plan to apply (over one shared donor pool, so two
    needs never both drain a donor); ``lines`` name each move for the email;
    ``sources`` (name + slack) drive the "what this leaves" summary;
    ``uncovered`` names the categories the pool couldn't reach. Empty ``moves``
    means nothing safe can cover anything.
    """

    moves: tuple[BudgetMove, ...]
    lines: tuple[CoverageLine, ...]
    sources: tuple[SourceView, ...]
    uncovered: tuple[str, ...] = ()

    @property
    def total(self) -> Money:
        """The amount this plan moves in total. Pure."""
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
    SLACK = "slack"
    DOES_NOT_COVER = "does_not_cover"


def _slack_limit(
    source: CategoryId,
    available: Mapping[CategoryId, Money],
    slack: Mapping[CategoryId, Money] | None,
) -> Money:
    """The protected drawable for a source: its slack, else its raw funds."""
    fallback = available.get(source, Money.zero())
    if slack is None:
        return fallback
    return slack.get(source, fallback)


def validate_option(
    option: BalanceOption,
    need: Need,
    available: Mapping[CategoryId, Money],
    policy: FloorPolicy = CAUTIOUS_FLOOR,
    *,
    slack: Mapping[CategoryId, Money] | None = None,
) -> OptionRejection | None:
    """Why ``option`` can't cover ``need``, or ``None`` if it can. Pure.

    The deterministic guard over the model's proposals (SPEC §8, §0.6): every
    move must fund the needy category with a positive amount within the per-move
    ceiling, the sources must actually hold what the moves pull (summed across
    moves from the same source), no source is pulled below its protected
    ``slack`` (what it can give after its own projected spend), and the moves
    together meet the shortfall. ``available`` is the real, current funds per
    source category; ``slack`` (when given) is the tighter protected cap — the
    source of truth the model's numbers are checked against.
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
        if amount > _slack_limit(source, available, slack):
            return OptionRejection.SLACK
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
    slack = {source.category: source.drawable for source in sources}
    return tuple(
        option
        for option in options
        if validate_option(option, need, available, policy, slack=slack) is None
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


def needs_from_assessments(
    assessments: Iterable[OverspendAssessment],
) -> tuple[Need, ...]:
    """The coordinated pass's needs, biggest shortfall first (SPEC §8). Pure.

    One need per over/trending assessment (positive shortfall only), ordered by
    shortfall descending — so the greedy plan covers the largest gaps first when
    the one shared pool can't cover them all (#46).
    """
    pending = [
        need
        for assessment in assessments
        if not (need := need_from_assessment(assessment)).is_met
    ]
    return tuple(
        sorted(
            pending, key=lambda n: (-n.shortfall.milliunits, str(n.category))
        )
    )


def donor_slack(spend: CategorySpend, clock: MonthClock) -> tuple[Money, Money]:
    """A donor's ``(slack, projection)`` for the month (SPEC §8). Pure.

    ``slack = max(0, balance - (projected - spent))`` — the donor's available
    funds (rollover included) less its own projected *remaining* spend, so it is
    never drained below what it still needs. "Over" is measured against
    available, exactly as a category's own overspend is (SPEC §7, #44): slack is
    the negative of :func:`need_from_assessment`'s shortfall, so a category that
    is itself over or trending has ``slack <= 0`` and is excluded as a donor —
    even one sitting on a thin or negative carried-in balance. A category with
    real carryover and little left to spend has the most slack, the safest donor
    there is. The second element is the donor's own projected month-end spend.
    """
    zero = Money.zero()
    projection = project_spend(spend, clock)
    remaining = projection - spent_magnitude(spend)  # own future spend (>= 0)
    slack = spend.balance - remaining
    if slack < zero:
        slack = zero
    return slack, projection


def sources_from_spends(
    spends: Iterable[CategorySpend],
    ready_to_assign: Money,
    *,
    exclude: frozenset[CategoryId],
    clock: MonthClock,
) -> tuple[Source, ...]:
    """The buckets the balancer may pull from, by slack (SPEC §8). Pure.

    Every category with positive *slack* (room after its own projected spend),
    minus the needy categories themselves (``exclude``), plus Ready-to-Assign
    when positive, which leads. A donor that is itself over or trending has a
    slack ``<= 0`` and is excluded — the balancer never robs a category that
    needs the money. One shared, slack-ranked pool serves a whole coordinated
    pass, so two needs can't both lay claim to the same donor (SPEC §8, #46).
    Each source carries its raw balance, its slack (the drawable cap), and its
    own projection for the model and the offer's after-state.
    """
    zero = Money.zero()
    sources: list[Source] = []
    if ready_to_assign > zero:
        sources.append(
            Source(
                category=READY_TO_ASSIGN_SOURCE,
                available=ready_to_assign,
                priority=SourcePriority.READY_TO_ASSIGN,
                slack=ready_to_assign,
                projection=zero,
            )
        )
    for spend in spends:
        if spend.category in exclude:
            continue
        slack, projection = donor_slack(spend, clock)
        if slack <= zero:
            continue
        sources.append(
            Source(
                category=spend.category,
                available=spend.balance,
                priority=SourcePriority.OVERFUNDED,
                slack=slack,
                projection=projection,
            )
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
    *,
    slack: Mapping[CategoryId, Money] | None = None,
) -> OptionRejection | None:
    """Why these owner-approved moves can't be applied, or ``None``. Pure.

    The apply-time guard: positive amounts within the per-move ceiling, no
    source pulled past its available funds, and none pulled below its protected
    ``slack`` (re-checked against current funds, which may have shifted since
    the offer). Unlike :func:`validate_option` it does NOT require covering the
    shortfall — the owner may choose to cover only part ("only $50").
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
        if amount > _slack_limit(source, available, slack):
            return OptionRejection.SLACK
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
