"""The rule-learning transitions: the tiny memory that earns autonomy (§9).

Pure. Given the current rule table and one :data:`LearningEvent`, return the
updated table plus a one-line record of what changed. The whole autonomy model
lives in three moves (SPEC §4.2, §9):

  * **Confirm** strengthens the rule for this preference: a learned rule climbs
    ``suggested → confirmed`` on the first confirmation and ``confirmed →
    trusted`` once it has ``K`` consistent hits of the *same* action. K is
    counted per rule, so confirming category B after A builds a *different*
    rule and an oscillating payee never reaches ``trusted`` (stays L0 — safe).
  * **Correct** rewrites the driving rule's action to the new preference and
    resets it to ``suggested`` (the new action is unproven). Corrections always
    win; a correction of a ruleless decision seeds a fresh suggested rule.
  * **Explicit command** blesses a rule straight to ``trusted`` /
    ``human_explicit`` — autonomy a human granted directly, not earned.

Autonomy is only ever earned by confirmation or granted by command, never by
inaction — there is no path here that raises trust on silence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import RuleSource, TrustState
from ynab_agent.domain.ids import RuleId
from ynab_agent.domain.rule import Rule
from ynab_agent.learn.events import (
    ConfirmCategory,
    CorrectDecision,
    ExplicitCommand,
)

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable

    from ynab_agent.learn.events import LearningEvent

# Consistent confirmations a learned rule needs to reach `trusted` (SPEC §4.2).
K_DEFAULT = 3


def trust_for_hits(hits: int, k_threshold: int) -> TrustState:
    """The trust a *learned* rule earns from its confirmation count alone."""
    if hits <= 0:
        return TrustState.SUGGESTED
    if hits >= k_threshold:
        return TrustState.TRUSTED
    return TrustState.CONFIRMED


class RuleChangeKind(StrEnum):
    """What a learning event did to the rule table (for the audit log)."""

    CREATED = "created"
    STRENGTHENED = "strengthened"
    REWRITTEN = "rewritten"
    BLESSED = "blessed"


class RuleChange(Frozen):
    """The single rule-table change a learning event produced."""

    kind: RuleChangeKind
    rule_id: RuleId
    trust: TrustState


class LearningOutcome(Frozen):
    """The updated rule table and the change that produced it (pure)."""

    rules: tuple[Rule, ...]
    change: RuleChange


def _find(rules: Iterable[Rule], rule_id: RuleId | None) -> Rule | None:
    if rule_id is None:
        return None
    return next((r for r in rules if r.id == rule_id), None)


def _replace(rules: tuple[Rule, ...], updated: Rule) -> tuple[Rule, ...]:
    return tuple(updated if r.id == updated.id else r for r in rules)


def apply_learning(
    rules: tuple[Rule, ...],
    event: LearningEvent,
    *,
    now: datetime.datetime,
    next_id: RuleId,
    k_threshold: int = K_DEFAULT,
) -> LearningOutcome:
    """Fold one learning event into the rule table (SPEC §9). Pure.

    Args:
        rules: The current rules (the handler loads the candidate set).
        event: The confirm / correct / explicit-command event.
        now: The decision time, stamped onto the touched rule.
        next_id: A fresh id to use if a new rule must be created (ignored
            otherwise — a pure function cannot mint one).
        k_threshold: Confirmations to reach ``trusted``. Defaults to ``K``.

    Returns:
        The updated table and the :class:`RuleChange` it made.
    """
    match event:
        case ConfirmCategory():
            return _confirm(
                rules, event, now=now, next_id=next_id, k=k_threshold
            )
        case CorrectDecision():
            return _correct(rules, event, now=now, next_id=next_id)
        case ExplicitCommand():
            return _bless(rules, event, now=now, next_id=next_id)
    assert_never(event)


def _confirm(
    rules: tuple[Rule, ...],
    event: ConfirmCategory,
    *,
    now: datetime.datetime,
    next_id: RuleId,
    k: int,
) -> LearningOutcome:
    target = _find(rules, event.rule_id)
    # The driver must encode the same preference; otherwise this confirms a new
    # action — fall through to find an equivalent rule or create one.
    if target is not None and target.action != event.action:
        target = None
    if target is None:
        target = next(
            (
                r
                for r in rules
                if r.match == event.match and r.action == event.action
            ),
            None,
        )

    if target is None:
        rule = Rule(
            id=next_id,
            match=event.match,
            action=event.action,
            trust=trust_for_hits(1, k),
            hits=1,
            last_confirmed_at=now,
            source=RuleSource.LEARNED,
        )
        return LearningOutcome(
            rules=(*rules, rule),
            change=RuleChange(
                kind=RuleChangeKind.CREATED, rule_id=rule.id, trust=rule.trust
            ),
        )

    hits = target.hits + 1
    # A human-blessed rule stays trusted; a learned one earns trust by hits.
    trust = (
        TrustState.TRUSTED
        if target.source is RuleSource.HUMAN_EXPLICIT
        else trust_for_hits(hits, k)
    )
    updated = target.model_copy(
        update={"hits": hits, "trust": trust, "last_confirmed_at": now}
    )
    return LearningOutcome(
        rules=_replace(rules, updated),
        change=RuleChange(
            kind=RuleChangeKind.STRENGTHENED,
            rule_id=updated.id,
            trust=updated.trust,
        ),
    )


def _correct(
    rules: tuple[Rule, ...],
    event: CorrectDecision,
    *,
    now: datetime.datetime,
    next_id: RuleId,
) -> LearningOutcome:
    target = _find(rules, event.prior_rule_id)
    if target is None:
        # The overturned decision had no rule: seed the new preference fresh.
        rule = Rule(
            id=next_id,
            match=event.match,
            action=event.action,
            trust=TrustState.SUGGESTED,
            hits=0,
            last_corrected_at=now,
            source=RuleSource.LEARNED,
        )
        return LearningOutcome(
            rules=(*rules, rule),
            change=RuleChange(
                kind=RuleChangeKind.CREATED, rule_id=rule.id, trust=rule.trust
            ),
        )

    # Rewrite the driving rule's action and demote: the new action is unproven,
    # so it restarts at suggested as a learned hypothesis (SPEC §9).
    updated = target.model_copy(
        update={
            "action": event.action,
            "trust": TrustState.SUGGESTED,
            "hits": 0,
            "last_corrected_at": now,
            "source": RuleSource.LEARNED,
        }
    )
    return LearningOutcome(
        rules=_replace(rules, updated),
        change=RuleChange(
            kind=RuleChangeKind.REWRITTEN,
            rule_id=updated.id,
            trust=updated.trust,
        ),
    )


def _bless(
    rules: tuple[Rule, ...],
    event: ExplicitCommand,
    *,
    now: datetime.datetime,
    next_id: RuleId,
) -> LearningOutcome:
    target = next(
        (
            r
            for r in rules
            if r.match == event.match and r.action == event.action
        ),
        None,
    )
    if target is None:
        rule = Rule(
            id=next_id,
            match=event.match,
            action=event.action,
            trust=TrustState.TRUSTED,
            hits=0,
            last_confirmed_at=now,
            source=RuleSource.HUMAN_EXPLICIT,
        )
        return LearningOutcome(
            rules=(*rules, rule),
            change=RuleChange(
                kind=RuleChangeKind.BLESSED, rule_id=rule.id, trust=rule.trust
            ),
        )

    updated = target.model_copy(
        update={
            "trust": TrustState.TRUSTED,
            "source": RuleSource.HUMAN_EXPLICIT,
            "last_confirmed_at": now,
        }
    )
    return LearningOutcome(
        rules=_replace(rules, updated),
        change=RuleChange(
            kind=RuleChangeKind.BLESSED,
            rule_id=updated.id,
            trust=updated.trust,
        ),
    )
