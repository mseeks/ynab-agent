"""The revision-interpreting agent: what should change now? (SPEC §3).

The agentic half of W2's ``converge`` step, which runs when an already-applied
transaction is being revised (a late correction, or a receipt that arrived after
the fact). Given the revision instruction — a human's reply or a parsed
receipt's facts — and the current category/memo, the agent decides the new
*target*: retarget to a different category, update only the memo, or no change
at all (the instruction was informational). It never decides whether the change
*landed* — that is the spine's commit-then-verify, which turns this target into
the domain ``ConvergeOutcome``.

Producing only a target (never a verification result) keeps the model out of the
read-back loop: a regenerated memo preserves the existing person-tag (§4.4), and
a no-op revision is recognised as such rather than re-written needlessly (§3).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.enrich import CandidateCategory
from ynab_agent.agentic.model import run_structured
from ynab_agent.domain.base import Frozen

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class RevisionDecision(StrEnum):
    """What a revision instruction asks to change."""

    RETARGET = "retarget"
    MEMO_ONLY = "memo_only"
    NO_CHANGE = "no_change"


class RevisionRequest(Frozen):
    """A revision instruction and the current state it revises."""

    instruction: str
    current_category_name: str
    candidates: tuple[CandidateCategory, ...] = Field(min_length=1)
    current_memo: str | None = None


class RevisionTarget(Frozen):
    """The agent's revision target (the spine then commits and verifies)."""

    decision: RevisionDecision
    category_id: str | None = None
    memo: str | None = None


class RevisionPlan(Frozen):
    """A revision target reconciled against the current state."""

    changes: bool
    category_id: str | None = None
    memo: str | None = None


_SYSTEM_PROMPT = """\
An already-categorized transaction is being revised. You are given the revision
instruction (a human reply or a forwarded receipt's facts), the category it
currently has, its current memo, and the candidate categories.

Decide the target: `retarget` if it should move to a different category — set
`category_id` to the matching candidate and, if the instruction implies one, a
`memo`; `memo_only` if just the memo should change — set `memo`; or `no_change`
if the instruction changes nothing (it was informational or already reflected).
Prefer `no_change` over re-writing the same thing — a needless write is worse
than none."""

_AGENT: Agent[None, RevisionTarget] = Agent(
    output_type=RevisionTarget,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: RevisionRequest) -> str:
    """Render the request as the agent's user prompt."""
    lines = [
        f"Instruction: {request.instruction}",
        f"Current category: {request.current_category_name}",
        f"Current memo: {request.current_memo or '(none)'}",
        "Candidate categories:",
    ]
    lines.extend(f"  - {c.name} (id: {c.id})" for c in request.candidates)
    return "\n".join(lines)


async def interpret_revision(
    request: RevisionRequest, *, model: Model | None = None
) -> RevisionTarget:
    """Run the revision-interpreting agent for one instruction (SPEC §3).

    Args:
        request: The revision instruction and the current state.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The agent's structured revision target.
    """
    return await run_structured(
        _AGENT,
        _format_request(request),
        output_type=RevisionTarget,
        model=model,
    )


def to_revision_plan(
    target: RevisionTarget, candidates: tuple[CandidateCategory, ...]
) -> RevisionPlan:
    """Reconcile the target into a plan, defaulting to no-change (SPEC §3).

    A ``retarget`` with no category — or with an id that is not a real
    candidate (a hallucination; the write would land wrong or 400) — collapses
    to "no change", as does any unrecognized shape: the spine never commits a
    write the model under-specified.
    """
    if (
        target.decision is RevisionDecision.RETARGET
        and target.category_id
        and any(c.id == target.category_id for c in candidates)
    ):
        return RevisionPlan(
            changes=True, category_id=target.category_id, memo=target.memo
        )
    if target.decision is RevisionDecision.MEMO_ONLY and target.memo:
        return RevisionPlan(changes=True, memo=target.memo)
    return RevisionPlan(changes=False)
