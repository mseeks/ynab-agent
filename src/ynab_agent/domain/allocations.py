"""Allocations: how a transaction's amount is assigned to categories.

The central rule the SPEC encodes (§1, §4.3): an allocation is *either* a single
category *or* a split — never both, never neither. We model that as a
discriminated union, so "a category and a split at once" cannot be constructed.

Two forms exist:

* **Proposed** allocations are templates. A split line's *share* is relative — a
  percent or a fixed amount — because a proposal/rule does not yet bind to a
  concrete transaction total. Per the SPEC, fixed lines are subtracted first and
  the remainder distributes across percent lines, so "$40 Gifts, rest Groceries"
  and "50/50" both encode deterministically.
* **Resolved** allocations are concrete: each split line carries an exact
  :class:`Money` amount (a YNAB subtransaction). A :class:`~.proposal.Decision`
  can only hold a resolved allocation, so an unresolved percent can never
  reach a YNAB write.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId, PersonTag
from ynab_agent.domain.money import Money

_FULL_PERCENT = 100
_MIN_SPLIT_LINES = 2


# ── Share specs (proposed, relative) ────────────────────────────────────────
class PercentShare(Frozen):
    """A whole-number percent of the distributable remainder."""

    kind: Literal["percent"] = "percent"
    percent: int = Field(ge=1, le=100)


class FixedShare(Frozen):
    """A fixed amount, subtracted before percent shares distribute."""

    kind: Literal["fixed"] = "fixed"
    amount: Money


ShareSpec = Annotated[PercentShare | FixedShare, Field(discriminator="kind")]


class SplitLine(Frozen):
    """One line of a proposed split."""

    share: ShareSpec
    category: CategoryId
    memo_template: str | None = None
    person_tag: PersonTag | None = None


# ── Proposed allocation (template) ──────────────────────────────────────────
class ProposedCategory(Frozen):
    """A whole-transaction category assignment."""

    kind: Literal["category"] = "category"
    category: CategoryId
    person_tag: PersonTag | None = None


class ProposedSplit(Frozen):
    """A multi-line split template."""

    kind: Literal["split"] = "split"
    lines: tuple[SplitLine, ...]

    @model_validator(mode="after")
    def _check_lines(self) -> ProposedSplit:
        if len(self.lines) < _MIN_SPLIT_LINES:
            msg = "a split needs at least two lines"
            raise ValueError(msg)
        percents = [
            line.share.percent
            for line in self.lines
            if isinstance(line.share, PercentShare)
        ]
        # Fixed lines are subtracted first; the remainder distributes across
        # percent lines (SPEC §1). So a template needs at least one percent line
        # to absorb the remainder, and those percents must partition it (sum to
        # 100). An all-fixed template has no remainder sink and is rejected.
        if not percents:
            msg = "a split needs a percent line to absorb the remainder"
            raise ValueError(msg)
        if sum(percents) != _FULL_PERCENT:
            msg = f"percent shares must sum to 100, got {sum(percents)}"
            raise ValueError(msg)
        return self


ProposedAllocation = Annotated[
    ProposedCategory | ProposedSplit, Field(discriminator="kind")
]


# ── Resolved allocation (concrete) ──────────────────────────────────────────
class ResolvedCategory(Frozen):
    """A concrete whole-transaction category assignment."""

    kind: Literal["category"] = "category"
    category: CategoryId
    person_tag: PersonTag | None = None


class ResolvedSplitLine(Frozen):
    """One concrete YNAB subtransaction."""

    category: CategoryId
    amount: Money
    memo: str | None = None
    person_tag: PersonTag | None = None


class ResolvedSplit(Frozen):
    """A concrete split: exact amounts that a YNAB write can use directly."""

    kind: Literal["split"] = "split"
    lines: tuple[ResolvedSplitLine, ...]

    @model_validator(mode="after")
    def _check_lines(self) -> ResolvedSplit:
        if len(self.lines) < _MIN_SPLIT_LINES:
            msg = "a split needs at least two lines"
            raise ValueError(msg)
        return self

    @property
    def total(self) -> Money:
        """The sum of the line amounts (should equal the transaction total)."""
        running = Money.zero()
        for line in self.lines:
            running = running + line.amount
        return running


ResolvedAllocation = Annotated[
    ResolvedCategory | ResolvedSplit, Field(discriminator="kind")
]
