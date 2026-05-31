"""Rule: a ``match → action`` with a trust state — the only memory that matters.

Rules are looked up by payee plus optional conditions (SPEC §1). The spine never
ranks competing rules; it asks one question — does exactly one trusted/blessed
rule clearly apply? — and otherwise routes to a human. A rule's *action* is a
template, so it carries a *proposed* (relative) allocation, not concrete
amounts.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ynab_agent.domain.allocations import ProposedAllocation
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import RuleSource, TrustState
from ynab_agent.domain.ids import AccountId, RuleId
from ynab_agent.domain.money import Money


class AmountRange(Frozen):
    """An inclusive amount band; either bound may be open."""

    low: Money | None = None
    high: Money | None = None

    @model_validator(mode="after")
    def _check_order(self) -> AmountRange:
        if (
            self.low is not None
            and self.high is not None
            and self.low > self.high
        ):
            msg = "amount range low must not exceed high"
            raise ValueError(msg)
        return self

    def contains(self, amount: Money) -> bool:
        """Whether ``amount`` falls within the band."""
        if self.low is not None and amount < self.low:
            return False
        return not (self.high is not None and amount > self.high)


class RuleMatch(Frozen):
    """The conditions under which a rule applies (SPEC §1)."""

    payee_pattern: str
    account: AccountId | None = None
    amount_range: AmountRange | None = None
    item_keyword: str | None = None


class RuleAction(Frozen):
    """What a rule proposes on match: an allocation plus an optional memo."""

    allocation: ProposedAllocation
    memo_template: str | None = None


class Rule(Frozen):
    """A learned or human-given categorization rule.

    Trust climbs ``suggested → confirmed → trusted`` by confirmation and is
    demoted by correction (SPEC §4.2, §9). Trust transitions themselves live in
    rule learning (W5); this type only records the current state.
    """

    id: RuleId
    match: RuleMatch
    action: RuleAction
    trust: TrustState
    hits: int = Field(default=0, ge=0)
    last_confirmed_at: datetime | None = None
    last_corrected_at: datetime | None = None
    source: RuleSource
