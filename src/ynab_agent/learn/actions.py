"""Turn a committed decision into a rule's template action (SPEC §9, §4.3).

A rule's action is a *proposed* (template) allocation, while a decision carries
a *resolved* (concrete) one. For a whole-category decision the template is the
same category, so the conversion is exact and deterministic. For a split, the
share template ("50/50" vs "$40 fixed, rest …") is a modelling judgment the
agentic handler makes — there is no single right way to read shares back out of
concrete amounts — so this returns ``None`` and the handler shapes the split.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ynab_agent.domain.allocations import ProposedCategory, ResolvedCategory
from ynab_agent.domain.rule import RuleAction

if TYPE_CHECKING:
    from ynab_agent.domain.proposal import Decision


def rule_action_from_decision(decision: Decision) -> RuleAction | None:
    """The rule template a decision implies, or ``None`` for a split.

    A whole-category decision converts exactly (same category, same person tag,
    memo carried as the template). A split needs the agent to choose its share
    template, so this declines it.
    """
    allocation = decision.allocation
    if isinstance(allocation, ResolvedCategory):
        return RuleAction(
            allocation=ProposedCategory(
                category=allocation.category,
                person_tag=allocation.person_tag,
            ),
            memo_template=decision.memo,
        )
    return None
