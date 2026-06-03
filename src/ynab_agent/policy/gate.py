"""The autonomy gate: whether a proposal may auto-apply (SPEC §4.2, §1).

Autonomy is authorized by *rules*, not raw model confidence (principle 6). The
spine does not rank competing rules; it asks one question — does exactly one
trusted rule clearly apply? If yes, that rule may gate auto-apply (still subject
to the hard floor). If it is ambiguous (conflicting trusted rules, or none
clearly applies), the transaction goes to ASK.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import DecidedBy, RuleSource, TrustState
from ynab_agent.domain.proposal import Decision
from ynab_agent.policy.floor import (
    CAUTIOUS_FLOOR,
    AutoActionCounters,
    FloorPolicy,
    FloorVerdict,
    check_floor,
)
from ynab_agent.policy.resolve import resolve_allocation

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable

    from ynab_agent.domain.rule import Rule
    from ynab_agent.domain.transaction import YnabSnapshot


class GateVerdict(StrEnum):
    """The autonomy gate's ruling."""

    AUTO = "auto"
    ASK = "ask"


class GateOutcome(Frozen):
    """The gate's decision and why.

    Attributes:
        verdict: ``AUTO`` (auto-apply eligible) or ``ASK`` (email a proposal).
        rule_id: The single gating rule's id when ``AUTO``; ``None`` otherwise.
        reason: A short, human-readable explanation (for the audit log).
    """

    verdict: GateVerdict
    rule_id: str | None = None
    reason: str = ""


def rule_matches(rule: Rule, snapshot: YnabSnapshot) -> bool:
    """Whether all of a rule's match conditions apply to a transaction (§1).

    Payee matching is case-insensitive substring containment; an amount range is
    compared in YNAB's signed convention; an item keyword is matched against the
    memo (where item detail lands).
    """
    match = rule.match
    if match.payee_pattern.lower() not in snapshot.payee.lower():
        return False
    if match.account is not None and match.account != snapshot.account:
        return False
    if match.amount_range is not None and not match.amount_range.contains(
        snapshot.amount
    ):
        return False
    if match.item_keyword is not None:
        memo = snapshot.memo or ""
        if match.item_keyword.lower() not in memo.lower():
            return False
    return True


def matching_rules(rules: Iterable[Rule], snapshot: YnabSnapshot) -> list[Rule]:
    """Return the rules whose conditions all apply to the transaction."""
    return [rule for rule in rules if rule_matches(rule, snapshot)]


def evaluate_gate(
    snapshot: YnabSnapshot,
    rules: Iterable[Rule],
    counters: AutoActionCounters,
    policy: FloorPolicy = CAUTIOUS_FLOOR,
) -> GateOutcome:
    """Decide whether the transaction may auto-apply (SPEC §4.2, §14). Pure.

    The hard floor is consulted first; it can only force ASK, never grant AUTO.
    Then exactly one *blessed* matching rule is required to gate auto-apply. Per
    the §14 opt-in on-ramp, a learned rule that reaches ``trusted`` by
    consistency is only *eligible* — it does not auto-apply until the owner
    blesses it (``source=human_explicit``). So autonomy is always granted, never
    taken: a trusted-but-unblessed rule still routes to ASK.
    """
    floor = check_floor(snapshot.amount, counters, policy)
    if floor is not FloorVerdict.ALLOW:
        return GateOutcome(verdict=GateVerdict.ASK, reason=f"floor: {floor}")

    blessed = [
        rule
        for rule in matching_rules(rules, snapshot)
        if rule.trust is TrustState.TRUSTED
        and rule.source is RuleSource.HUMAN_EXPLICIT
    ]
    if len(blessed) == 1:
        return GateOutcome(
            verdict=GateVerdict.AUTO,
            rule_id=blessed[0].id,
            reason="single blessed rule clearly applies",
        )
    if not blessed:
        return GateOutcome(
            verdict=GateVerdict.ASK, reason="no blessed rule applies"
        )
    return GateOutcome(
        verdict=GateVerdict.ASK, reason="conflicting blessed rules"
    )


def build_auto_decision(
    rule: Rule, snapshot: YnabSnapshot, decided_at: datetime.datetime
) -> Decision:
    """Resolve a gating rule's action into an approved agent decision.

    Args:
        rule: The single trusted rule the gate selected.
        snapshot: The transaction to bind the rule's template against.
        decided_at: The decision timestamp (passed in; the core reads no clock).

    Returns:
        An approved, agent-decided :class:`~.proposal.Decision`.
    """
    allocation = resolve_allocation(rule.action.allocation, snapshot.amount)
    return Decision(
        allocation=allocation,
        memo=rule.action.memo_template,
        approved=True,
        decided_by=DecidedBy.AGENT,
        decided_at=decided_at,
        rule_id=rule.id,
    )
