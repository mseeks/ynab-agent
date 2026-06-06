"""The hard floor: deterministic limits no model can invade (SPEC §0.6 Layer 1).

These bound catastrophe regardless of the model — a runaway poller, a prompt
injection, or a model meltdown must not get past them. They are pure and
independent of trust: above them, autonomy is irrelevant.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.money import Money


class FloorVerdict(StrEnum):
    """The hard floor's ruling on a proposed auto-write."""

    ALLOW = "allow"
    FORCE_HUMAN = "force_human"
    TRIP_BREAKER = "trip_breaker"


class FloorPolicy(Frozen):
    """Tunable hard-floor limits (SPEC §0.6, §11).

    The ceiling is a magnitude: a transaction whose absolute amount exceeds it
    always drops to a human, regardless of trust.
    """

    per_txn_ceiling: Money = Field(
        default_factory=lambda: Money.from_currency(75)
    )
    per_run_cap: int = Field(default=8, ge=0)
    per_day_cap: int = Field(default=20, ge=0)
    # Budget reallocations (W7, SPEC §8) ride the same floor. A move is larger
    # than a typical txn write (it covers a whole category's shortfall), so it
    # gets its own, higher ceiling; the daily cap bounds how many the agent
    # applies in a day even when each is confirmed.
    per_move_ceiling: Money = Field(
        default_factory=lambda: Money.from_currency(500)
    )
    moves_per_day_cap: int = Field(default=10, ge=0)


CAUTIOUS_FLOOR = FloorPolicy()


class AutoActionCounters(Frozen):
    """Auto-actions already taken this run and today (spine-maintained)."""

    this_run: int = Field(default=0, ge=0)
    today: int = Field(default=0, ge=0)


def check_floor(
    amount: Money | None,
    counters: AutoActionCounters,
    policy: FloorPolicy = CAUTIOUS_FLOOR,
) -> FloorVerdict:
    """Rule on whether an auto-write is permitted by the hard floor.

    Order matters: an unreadable or over-ceiling amount forces a human before
    the circuit breaker is even consulted.

    Args:
        amount: The transaction amount, or ``None`` if it could not be read.
        counters: Auto-actions already taken this run and today.
        policy: The configured limits.

    Returns:
        ``FORCE_HUMAN`` for an unreadable or over-ceiling amount;
        ``TRIP_BREAKER`` when a per-run or per-day cap is reached; else
        ``ALLOW``.
    """
    if amount is None:
        # Never act on an unreadable amount (SPEC §0.6 Layer 1, rule 3).
        return FloorVerdict.FORCE_HUMAN
    if abs(amount) > policy.per_txn_ceiling:
        return FloorVerdict.FORCE_HUMAN
    if (
        counters.this_run >= policy.per_run_cap
        or counters.today >= policy.per_day_cap
    ):
        return FloorVerdict.TRIP_BREAKER
    return FloorVerdict.ALLOW


def check_budget_move_floor(
    amount: Money,
    counters: AutoActionCounters,
    policy: FloorPolicy = CAUTIOUS_FLOOR,
) -> FloorVerdict:
    """Rule on whether a single budget reallocation move is permitted (SPEC §8).

    The same uninvadeable floor as categorization, sized for reallocations. A
    move whose magnitude exceeds the per-move ceiling drops to a human *even
    when the owner confirmed it* — the floor never trusts a number it would
    refuse to write on its own. The daily cap then trips the breaker.

    Args:
        amount: The move's magnitude (a positive reallocation amount).
        counters: Moves already applied today (``today``); ``this_run`` is
            unused here — a balancer pass applies at most a handful of moves.
        policy: The configured limits.

    Returns:
        ``FORCE_HUMAN`` for an over-ceiling move; ``TRIP_BREAKER`` when the
        daily cap is reached; else ``ALLOW``.
    """
    if abs(amount) > policy.per_move_ceiling:
        return FloorVerdict.FORCE_HUMAN
    if counters.today >= policy.moves_per_day_cap:
        return FloorVerdict.TRIP_BREAKER
    return FloorVerdict.ALLOW
