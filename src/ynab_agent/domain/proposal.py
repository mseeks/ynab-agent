"""Proposal and Decision: the agent's best guess, and the committed outcome.

A :class:`Proposal` is recomputed on every signal — a *proposed* (template)
allocation plus framing (confidence, rationale, sources). A :class:`Decision` is
the committed result: a *resolved* (concrete) allocation that a YNAB write can
use, stamped with who decided it and when. Because a Decision can only carry a
resolved allocation, an unresolved percent share can never reach a write.
"""

from __future__ import annotations

from datetime import datetime

from ynab_agent.domain.allocations import ProposedAllocation, ResolvedAllocation
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import Confidence, DecidedBy, SourceKind
from ynab_agent.domain.ids import CategoryId, RuleId


class ProposalSource(Frozen):
    """One signal that fed a proposal (SPEC §4.1)."""

    kind: SourceKind
    detail: str | None = None


class Proposal(Frozen):
    """The current best guess for how to categorize a transaction.

    Confidence is framing only — it shapes wording and the ask, never whether a
    human is required (the §4.2 ladder is the sole autonomy gate).
    """

    allocation: ProposedAllocation
    memo: str | None = None
    confidence: Confidence
    rationale: str
    sources: tuple[ProposalSource, ...] = ()
    # A few runner-up categories the model also considered, surfaced in the
    # proposal email so the owner can pick one at a glance (framing only).
    alternatives: tuple[CategoryId, ...] = ()


class Decision(Frozen):
    """A committed categorization decision.

    Attributes:
        allocation: The concrete category or split written to YNAB.
        memo: The memo written (per subtransaction when split, via the lines).
        approved: Whether the transaction was approved in YNAB.
        decided_by: A human reply, or the agent under a blessed rule.
        decided_at: When the decision was made (an absolute timestamp).
        rule_id: The rule that drove the decision; ``None`` for a pure-human
            decision with no rule yet.
    """

    allocation: ResolvedAllocation
    memo: str | None = None
    approved: bool
    decided_by: DecidedBy
    decided_at: datetime
    rule_id: RuleId | None = None
