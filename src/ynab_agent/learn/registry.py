"""The durable rule registry's pure state and folds (SPEC §14, W5).

The registry is the agent's *only* persistent learning memory: the rule table
that earns autonomy. Per the derived-state rule (SPEC §0.5) it lives as Temporal
workflow state — never an external store — and everything else (what a payee
*usually* maps to, how consistent it has been) is derived from YNAB's canonical
history on demand, not duplicated here.

This module is the pure core the durable :class:`RuleRegistryWorkflow` wraps
(mirroring how ``state_machine`` sits under ``txn_workflow``): given the current
:class:`RegistryState` and one learning input, return the next state. It folds
in the already-pure transition logic (``plan_rule_update`` / ``apply_learning``)
and keeps a bounded audit tail so "why did it learn that?" is answerable without
an external log.

Autonomy is opt-in (SPEC §14): a *learned* rule that reaches ``trusted`` by
consistency is only **eligible** for auto-apply — :func:`eligible_for_bless`
surfaces it for the one-time owner prompt — and the gate auto-applies it only
once the owner blesses it (``source=human_explicit``). Nothing here grants
autonomy on silence.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import RuleSource, TrustState
from ynab_agent.domain.rule import Rule
from ynab_agent.learn.handler import plan_rule_update
from ynab_agent.learn.transitions import RuleChange, apply_learning

if TYPE_CHECKING:
    from ynab_agent.domain.effects import FeedRuleLearning
    from ynab_agent.domain.ids import RuleId
    from ynab_agent.learn.events import ExplicitCommand

# How many recent rule-table changes the registry keeps for the in-band audit
# (SPEC §9's interim log until the externalized audit subsystem lands). The tail
# is bounded so continued learning never grows the carried state without limit.
AUDIT_CAP = 200


class RegistryAuditEntry(Frozen):
    """One rule-table change, time-stamped and keyed to its payee (SPEC §9)."""

    at: datetime.datetime
    payee: str
    change: RuleChange


class RegistryState(Frozen):
    """The learned rule table plus a bounded change history.

    The whole of the agent's persistent learning memory. Held as Temporal
    workflow state and carried across continue-as-new, so it is a frozen value
    like the rest of the domain.
    """

    rules: tuple[Rule, ...] = ()
    audit: tuple[RegistryAuditEntry, ...] = Field(default=())


def _appended_audit(
    state: RegistryState, entry: RegistryAuditEntry
) -> tuple[RegistryAuditEntry, ...]:
    """The audit tail with ``entry`` appended, capped to :data:`AUDIT_CAP`."""
    return (*state.audit, entry)[-AUDIT_CAP:]


def record_learning(
    state: RegistryState,
    feed: FeedRuleLearning,
    *,
    now: datetime.datetime,
    next_id: RuleId,
) -> RegistryState:
    """Fold a W2 confirm/correct effect into the registry (SPEC §9). Pure.

    Returns the state unchanged when there is nothing learnable (no decision,
    or a split whose share template is the agent's call — ``plan_rule_update``).
    """
    outcome = plan_rule_update(state.rules, feed, now=now, next_id=next_id)
    if outcome is None:
        return state
    entry = RegistryAuditEntry(at=now, payee=feed.payee, change=outcome.change)
    return state.model_copy(
        update={"rules": outcome.rules, "audit": _appended_audit(state, entry)}
    )


def bless_rule(
    state: RegistryState,
    command: ExplicitCommand,
    *,
    now: datetime.datetime,
    next_id: RuleId,
) -> RegistryState:
    """Bless a ``(match, action)`` straight to ``trusted`` (SPEC §14). Pure.

    The owner's opt-in: an eligible learned rule becomes auto-applicable, or an
    explicit standing command ("always categorize X as Y") seeds one already
    trusted. Either way ``source`` becomes ``human_explicit`` — granted, not
    earned — which is what the gate requires before it acts alone.
    """
    outcome = apply_learning(state.rules, command, now=now, next_id=next_id)
    entry = RegistryAuditEntry(
        at=now, payee=command.match.payee_pattern, change=outcome.change
    )
    return state.model_copy(
        update={"rules": outcome.rules, "audit": _appended_audit(state, entry)}
    )


def eligible_for_bless(state: RegistryState) -> tuple[Rule, ...]:
    """Learned rules that reached ``trusted`` by consistency but aren't blessed.

    These are the on-ramp surface (SPEC §14): a payee the owner has confirmed
    enough times that the agent *could* take it over, awaiting the one-time
    "want me to auto-handle this from now on?" opt-in. A rule already blessed
    (``human_explicit``) is excluded — it is past eligibility, already trusted.
    """
    return tuple(
        rule
        for rule in state.rules
        if rule.trust is TrustState.TRUSTED
        and rule.source is RuleSource.LEARNED
    )


def rules_for_payee(state: RegistryState, payee: str) -> tuple[Rule, ...]:
    """The rules whose payee pattern matches ``payee`` (case-insensitive).

    The gate's load path: it asks the registry for the candidate rules on a
    transaction's payee, then decides auto-vs-ask over just those.
    """
    lowered = payee.lower()
    return tuple(
        rule
        for rule in state.rules
        if rule.match.payee_pattern.lower() in lowered
    )
