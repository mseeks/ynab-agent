"""Build audit events from spine outputs, and explain a log (SPEC §9).

The spine already produces the facts worth recording — a :class:`GateOutcome`, a
:class:`Decision`, a :class:`RuleChange`, a state transition, an outbound send.
These adapters turn each into the matching :mod:`~ynab_agent.audit.entry` event
(the workflow appends them as it runs), and :func:`explain` renders a whole log
into the human-readable "why did it do that" trail.

Pure: no clock, no I/O. The caller supplies the timestamp when appending.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from ynab_agent.audit.entry import (
    AuditEvent,
    BudgetMoveApplied,
    Decided,
    Gated,
    Learned,
    MessageSent,
    StateChanged,
)
from ynab_agent.domain.allocations import ResolvedCategory
from ynab_agent.domain.money import Money

if TYPE_CHECKING:
    from ynab_agent.audit.entry import AuditLog
    from ynab_agent.budget.balance import BudgetMove
    from ynab_agent.domain.allocations import ResolvedAllocation
    from ynab_agent.domain.enums import TxnState
    from ynab_agent.domain.proposal import Decision
    from ynab_agent.learn.transitions import RuleChange
    from ynab_agent.policy.gate import GateOutcome


def allocation_summary(allocation: ResolvedAllocation) -> str:
    """A one-line description of what a decision allocated."""
    if isinstance(allocation, ResolvedCategory):
        return f"category {allocation.category}"
    return f"split across {len(allocation.lines)} lines"


def record_transition(
    *, to_state: TxnState, trigger: str, from_state: TxnState | None = None
) -> StateChanged:
    """A lifecycle transition, named by the event that caused it."""
    return StateChanged(
        to_state=to_state, trigger=trigger, from_state=from_state
    )


def record_gate(outcome: GateOutcome) -> Gated:
    """The autonomy gate's ruling (verdict, gating rule, reason)."""
    return Gated(
        verdict=outcome.verdict.value,
        reason=outcome.reason,
        rule_id=outcome.rule_id,
    )


def record_decision(decision: Decision) -> Decided:
    """A committed decision: what, who, approval, and the gating rule."""
    return Decided(
        decided_by=decision.decided_by,
        approved=decision.approved,
        summary=allocation_summary(decision.allocation),
        rule_id=decision.rule_id,
    )


def record_send(action_seq: int, purpose: str) -> MessageSent:
    """An outbound thread message, by its idempotency key and purpose."""
    return MessageSent(action_seq=action_seq, purpose=purpose)


def record_learning(change: RuleChange) -> Learned:
    """A rule-learning update: what changed and the rule's new trust."""
    return Learned(
        change=change.kind.value, rule_id=change.rule_id, trust=change.trust
    )


def record_budget_move(move: BudgetMove, month: str) -> BudgetMoveApplied:
    """A reallocation that was applied: amount moved source -> destination."""
    return BudgetMoveApplied(
        source=str(move.source),
        destination=str(move.destination),
        amount_milliunits=move.amount.milliunits,
        month=month,
    )


def _render_event(event: AuditEvent) -> str:
    match event:
        case StateChanged(to_state=to, trigger=trig, from_state=frm):
            origin = f"{frm} " if frm is not None else ""
            return f"state {origin}-> {to} (on {trig})"
        case Gated(verdict=verdict, reason=reason, rule_id=rule_id):
            via = f" via rule {rule_id}" if rule_id else ""
            tail = f" - {reason}" if reason else ""
            return f"gate: {verdict}{via}{tail}"
        case Decided(decided_by=who, approved=ok, summary=what, rule_id=rid):
            via = f" via rule {rid}" if rid else ""
            state = "approved" if ok else "unapproved"
            return f"decided {what} by {who} ({state}){via}"
        case MessageSent(action_seq=seq, purpose=purpose):
            return f"sent message #{seq}: {purpose}"
        case Learned(change=change, rule_id=rule_id, trust=trust):
            return f"learned: {change} rule {rule_id} -> {trust}"
        case BudgetMoveApplied(
            source=src, destination=dst, amount_milliunits=amt, month=month
        ):
            moved = Money.from_milliunits(amt)
            return f"budget move: {moved} {src} -> {dst} ({month})"
    assert_never(event)


def explain(log: AuditLog) -> str:
    """Render a log as the human-readable "why did it do that" trail (§9)."""
    return "\n".join(
        f"#{entry.seq:>2} {entry.at:%Y-%m-%d %H:%M}  "
        f"{_render_event(entry.event)}"
        for entry in log.entries
    )
