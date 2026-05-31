"""Adapt a W2 FeedRuleLearning effect into a rule-table update (SPEC §9).

This is the seam between the transaction lifecycle and the learning core. W2
emits a :class:`~ynab_agent.domain.effects.FeedRuleLearning` effect on every
human confirm/correct; :func:`plan_rule_update` turns that raw effect into the
right :data:`~ynab_agent.learn.events.LearningEvent` and folds it in via
:func:`~ynab_agent.learn.transitions.apply_learning`. Pure: the W5 activity
supplies the loaded rules, the clock, and a fresh id, and persists the result.

It declines (returns ``None``) when there is nothing to learn — no decision to
encode, or a split, whose share template is the agent's call (SPEC §4.3), not a
deterministic read-back of concrete amounts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from ynab_agent.domain.effects import RuleLearningKind
from ynab_agent.domain.rule import RuleMatch
from ynab_agent.learn.actions import rule_action_from_decision
from ynab_agent.learn.events import ConfirmCategory, CorrectDecision
from ynab_agent.learn.transitions import apply_learning

if TYPE_CHECKING:
    import datetime

    from ynab_agent.domain.effects import FeedRuleLearning
    from ynab_agent.domain.ids import RuleId
    from ynab_agent.domain.rule import Rule
    from ynab_agent.learn.transitions import LearningOutcome


def plan_rule_update(
    rules: tuple[Rule, ...],
    feed: FeedRuleLearning,
    *,
    now: datetime.datetime,
    next_id: RuleId,
) -> LearningOutcome | None:
    """Fold a W2 learning effect into the rule table, or ``None`` if nothing.

    Args:
        rules: The current rules for this payee (the activity loads them).
        feed: The W2 confirm/correct effect.
        now: The decision time, stamped onto the touched rule.
        next_id: A fresh id for a newly created rule (ignored otherwise).

    Returns:
        The rule-table change, or ``None`` when there is nothing learnable
        (no decision, or a split awaiting the agent's share template).
    """
    if feed.decision is None:
        return None
    action = rule_action_from_decision(feed.decision)
    if action is None:
        return None
    match = RuleMatch(payee_pattern=feed.payee)

    match feed.event:
        case RuleLearningKind.CONFIRM:
            event = ConfirmCategory(
                match=match, action=action, rule_id=feed.decision.rule_id
            )
            return apply_learning(rules, event, now=now, next_id=next_id)
        case RuleLearningKind.CORRECT:
            prior_rule_id = (
                feed.prior.rule_id if feed.prior is not None else None
            )
            correction = CorrectDecision(
                match=match, action=action, prior_rule_id=prior_rule_id
            )
            return apply_learning(rules, correction, now=now, next_id=next_id)
    assert_never(feed.event)
