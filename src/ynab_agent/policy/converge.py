"""Converge-to-target reconciliation for REVISING (SPEC §3 rules 2-4).

REVISING is the riskiest path: it mutates approved money records. The spine
converges to a single target end-state — read current YNAB state, write only if
it differs, then verify field-by-field. These pure helpers compute the three
decisions that procedure needs: the dedup hash, whether a write is needed, and
how a read-after-write compares to the target.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
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
    # A target with no memo means "no memo intent — leave it alone" (the agent
    # never clears a memo), so the read-back's memo must not count against the
    # match: a bare "Gifts" reply on a transaction carrying an Amazon item
    # list used to diverge on every single write because None != that memo.
    if target.memo is None:
        read_back = read_back.model_copy(update={"memo": None})
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


class PrecommitAction(StrEnum):
    """The pre-write decision for a converge run (SPEC §3 rules 3-4)."""

    WRITE = "write"
    NO_CHANGE = "no_change"
    ALREADY_TARGET = "already_target"
    DIVERGED = "diverged"


def precommit_action(
    current: TargetState | None,
    target: TargetState,
    prior: TargetState | None,
) -> PrecommitAction:
    """Decide what to do *before* writing, from a fresh read of current state.

    This is SPEC §3 rule 3 (converge-to-target: read current YNAB state, write
    only if it differs) plus the *pre-write* half of rule 4 (a spouse edited it
    directly → don't clobber). Reading current state before the commit is what
    lets the spine skip a needless write — and surface a divergence — instead of
    overwriting it first and only noticing on the read-back.

    Args:
        current: The freshly-read current end-state, or ``None`` when there
            is no single category to read (a split, or uncategorized).
        target: The end-state the instruction asks for.
        prior: The end-state the agent last applied — the divergence baseline —
            or ``None`` when the txn was never applied (a revision from LAPSED).

    Returns:
        ``NO_CHANGE`` when YNAB already holds exactly the prior state and the
        instruction asks for nothing new (no write, return to rest);
        ``ALREADY_TARGET`` when the target is already in place but differs from
        the prior — a retried converge whose write landed, or an edit that
        happens to match — so adopt it as re-applied without rewriting;
        ``DIVERGED`` when YNAB drifted to a different non-empty category
        than the agent last applied (don't clobber, ask which wins);
        otherwise ``WRITE``.
    """
    if not needs_write(current, target):
        # YNAB already holds the target. A genuine no-op (unchanged from the
        # prior) returns to rest; a target that differs from the prior is a
        # write that already landed — adopt it rather than re-applying.
        if (
            current is not None
            and prior is not None
            and content_hash(current) == content_hash(prior)
        ):
            return PrecommitAction.NO_CHANGE
        return PrecommitAction.ALREADY_TARGET
    # A write is needed. Divergence is judged on the *allocation* (matching the
    # silent-edit guard, §14.2): a spouse recategorising out of band leaves a
    # different non-empty category than the agent last applied — surface it,
    # don't overwrite. A memo-only drift is not a divergence.
    if (
        current is not None
        and prior is not None
        and current.allocation != prior.allocation
    ):
        return PrecommitAction.DIVERGED
    return PrecommitAction.WRITE
