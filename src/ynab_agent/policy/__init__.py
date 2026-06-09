"""Pure decision policies: the deterministic, spine-owned half of the agent.

These functions compute *whether* and *what-end-state* — the autonomy gate
(§4.2), the hard floor (§0.6), the allocation resolver (§1), and the
converge-to-target reconciliation (§3) — feeding the outcomes the state machine
consumes. They are pure and depend only on the domain core. The agent-powered
safety review and the amount-anomaly heuristic live in the Pydantic AI layer.
"""

from __future__ import annotations

from ynab_agent.policy.converge import (
    PrecommitAction,
    TargetState,
    classify_verify,
    content_hash,
    needs_write,
    precommit_action,
    reconciliation_blocks,
    target_of,
)
from ynab_agent.policy.floor import (
    CAUTIOUS_FLOOR,
    AutoActionCounters,
    FloorPolicy,
    FloorVerdict,
    check_budget_move_floor,
    check_floor,
)
from ynab_agent.policy.gate import (
    GateOutcome,
    GateVerdict,
    build_auto_decision,
    evaluate_gate,
    matching_rules,
    rule_matches,
)
from ynab_agent.policy.resolve import resolve_allocation

__all__ = [
    "CAUTIOUS_FLOOR",
    "AutoActionCounters",
    "FloorPolicy",
    "FloorVerdict",
    "GateOutcome",
    "GateVerdict",
    "PrecommitAction",
    "TargetState",
    "build_auto_decision",
    "check_budget_move_floor",
    "check_floor",
    "classify_verify",
    "content_hash",
    "evaluate_gate",
    "matching_rules",
    "needs_write",
    "precommit_action",
    "reconciliation_blocks",
    "resolve_allocation",
    "rule_matches",
    "target_of",
]
