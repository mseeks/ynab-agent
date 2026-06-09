"""Converge-to-target reconciliation for REVISING (SPEC §3 rules 2-4).

REVISING is the riskiest path: it mutates approved money records. The spine
converges to a single target end-state — read current YNAB state, write only if
it differs, then verify field-by-field. These pure helpers compute the three
decisions that procedure needs: the dedup hash, whether a write is needed, and
how a read-after-write compares to the target.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ynab_agent.domain.allocations import (
    ResolvedAllocation,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.events import VerifyOutcome

if TYPE_CHECKING:
    from ynab_agent.domain.proposal import Decision
    from ynab_agent.domain.transaction import YnabSnapshot


class TargetState(Frozen):
    """The normalized end-state a write converges to.

    Only the fields a write actually sets — the allocation, the memo, and the
    approved flag — so two decisions that produce the same YNAB end-state hash
    identically (and dedup), regardless of incidental metadata.
    """

    allocation: ResolvedAllocation
    memo: str | None = None
    approved: bool = True


def target_of(decision: Decision) -> TargetState:
    """Project a decision onto its YNAB end-state."""
    return TargetState(
        allocation=decision.allocation,
        memo=decision.memo,
        approved=decision.approved,
    )


def _split_line_key(line: ResolvedSplitLine) -> tuple[str, int, str]:
    """A canonical sort key for a split line (category, amount, memo)."""
    return (str(line.category), line.amount.milliunits, line.memo or "")


def _canonical(target: TargetState) -> TargetState:
    """Put a split's lines in a canonical order before hashing.

    YNAB does not promise to return a split's subtransactions in the order they
    were written, so two equal splits must hash the same regardless of order. A
    whole-category target is already canonical.
    """
    allocation = target.allocation
    if isinstance(allocation, ResolvedSplit):
        ordered = tuple(sorted(allocation.lines, key=_split_line_key))
        return target.model_copy(
            update={
                "allocation": allocation.model_copy(update={"lines": ordered})
            }
        )
    return target


def content_hash(target: TargetState) -> str:
    """A stable hash of the end-state, for ``(ynab_id, content_hash)`` dedup.

    Deterministic across processes (Pydantic JSON with fixed field order and
    integer milliunits), so it is safe under Temporal replay. Split lines are
    canonically ordered first, so a read-back that reorders them still hashes
    equal to the target (SPEC §3 r4).
    """
    return hashlib.sha256(
        _canonical(target).model_dump_json().encode()
    ).hexdigest()


def needs_write(current: TargetState | None, target: TargetState) -> bool:
    """Whether YNAB must be written (SPEC §3 rule 3).

    ``False`` only when the current state already equals the target — the
    no-op exit that skips a needless intermediate write.
    """
    return current is None or content_hash(current) != content_hash(target)


def classify_verify(
    read_back: TargetState | None, target: TargetState
) -> VerifyOutcome:
    """Compare a read-after-write against the target (SPEC §3 rule 4).

    Args:
        read_back: The post-write YNAB state, or ``None`` if it could not be
            read after retries.
        target: The intended end-state.

    Returns:
        ``MATCH`` when they agree, ``COULD_NOT_CONFIRM`` when the read failed,
        else ``DIVERGED`` (YNAB shows a different non-empty state).
    """
    if read_back is None:
        return VerifyOutcome.COULD_NOT_CONFIRM
    if content_hash(read_back) == content_hash(target):
        return VerifyOutcome.MATCH
    return VerifyOutcome.DIVERGED


def reconciliation_blocks(snapshot: YnabSnapshot) -> bool:
    """Whether the reconciliation guard forbids a silent edit (SPEC §3 rule 2).

    A transaction that is reconciled *or in a closed month* must not be silently
    un-approved or edited; the spine should propose to the human (an additive
    correction) instead.
    """
    return snapshot.reconciled or snapshot.month_closed
