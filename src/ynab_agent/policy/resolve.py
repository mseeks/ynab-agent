"""Resolve a proposed (template) allocation into concrete amounts (SPEC §1).

A proposal or rule action is a *template* whose split shares are relative
(percent or fixed). Binding it to a concrete transaction total is deterministic:
fixed lines are subtracted first, then the remainder distributes across the
percent lines by their percents, with the last percent line absorbing any
integer-milliunit residue so the lines sum *exactly* to the total.

Fixed amounts are authored as positive magnitudes ("$40 Gifts"), so they are
applied in the transaction's direction — a fixed line of an outflow is itself an
outflow — rather than by raw sign (SPEC §1, §4.3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ynab_agent.domain.allocations import (
    FixedShare,
    PercentShare,
    ProposedCategory,
    ProposedSplit,
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.money import Money

if TYPE_CHECKING:
    from ynab_agent.domain.allocations import (
        ProposedAllocation,
        ResolvedAllocation,
    )


def resolve_allocation(
    proposed: ProposedAllocation, total: Money
) -> ResolvedAllocation:
    """Bind a proposed allocation to a transaction total.

    Args:
        proposed: A category or split template.
        total: The transaction amount the split must sum to.

    Returns:
        A concrete allocation; for a split, its line amounts sum to ``total``.
    """
    if isinstance(proposed, ProposedCategory):
        return ResolvedCategory(
            category=proposed.category, person_tag=proposed.person_tag
        )
    return _resolve_split(proposed, total)


def _resolve_split(split: ProposedSplit, total: Money) -> ResolvedSplit:
    # Apply fixed magnitudes in the transaction's direction (outflow vs inflow),
    # so a positive "$40 Gifts" on a -$120 outflow becomes a -$40 line.
    direction = -1 if total.milliunits < 0 else 1

    fixed_total = 0
    for line in split.lines:
        if isinstance(line.share, FixedShare):
            fixed_total += direction * abs(line.share.amount.milliunits)
    remainder = total.milliunits - fixed_total

    percent_positions = [
        i
        for i, line in enumerate(split.lines)
        if isinstance(line.share, PercentShare)
    ]
    last_percent = percent_positions[-1]

    resolved: list[ResolvedSplitLine] = []
    distributed = 0
    for i, line in enumerate(split.lines):
        share = line.share
        if isinstance(share, FixedShare):
            amount = direction * abs(share.amount.milliunits)
        elif i == last_percent:
            # The last percent line absorbs the residue, so the sum is exact.
            amount = remainder - distributed
        else:
            amount = remainder * share.percent // 100
            distributed += amount
        resolved.append(
            ResolvedSplitLine(
                category=line.category,
                amount=Money.from_milliunits(amount),
                memo=line.memo_template,
                person_tag=line.person_tag,
            )
        )
    return ResolvedSplit(lines=tuple(resolved))
