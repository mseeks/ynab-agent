"""Render a transaction's email body (SPEC §5).

A deterministic template — *not* a model call. The model already did the
thinking upstream (the proposal's category + one-line rationale + alternatives);
this just lays it out cleanly so a glance is enough to act. Keeping it templated
(rather than free-form prose) is a deliberate low-cognitive-load choice, and it
drops a per-email model round-trip.

The layout, for a proposal:

    Hulu — $13.07 — May 29
    <memo, when present — e.g. an Amazon item list>

    Suggested: Entertainment   (or: Streaming, Fun Money)
    recurring streaming subscription

    Just reply in your own words — confirm it, suggest a different category, …
"""

from __future__ import annotations

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.effects import MessagePurpose

_REPLY_HINT = (
    "Just reply in your own words — confirm it, suggest a different "
    "category, or ask a question."
)


class ComposeRequest(Frozen):
    """The facts the template lays out for one transaction email."""

    purpose: str  # the MessagePurpose value: proposal / confirm / clarify / ...
    payee: str
    amount_display: str
    txn_date: str  # already display-formatted (e.g. "May 29")
    memo: str | None = None
    proposed_category: str | None = None  # the best-guess category NAME
    alternatives: tuple[str, ...] = ()  # other category names to offer
    rationale: str | None = None  # one-line reason for the best guess
    question: str | None = (
        None  # an explicit question, when the purpose has one
    )


def _facts(request: ComposeRequest) -> str:
    """The transaction header line, plus the memo line when there is one."""
    header = f"{request.payee} — {request.amount_display} — {request.txn_date}"
    if request.memo and request.memo.strip():
        return f"{header}\n{request.memo.strip()}"
    return header


def _proposal_body(request: ComposeRequest, facts: str) -> str:
    suggested = (
        f"Suggested: {request.proposed_category or '(needs a category)'}"
    )
    if request.alternatives:
        suggested += f"   (or: {', '.join(request.alternatives)})"
    lines = [facts, "", suggested]
    if request.rationale:
        lines.append(request.rationale)
    lines += ["", _REPLY_HINT]
    return "\n".join(lines)


def render_body(request: ComposeRequest) -> str:
    """Lay out the email body for one transaction message (SPEC §5).

    The subject is templated separately by the caller; this is the body.
    """
    facts = _facts(request)
    if request.purpose == MessagePurpose.PROPOSAL.value:
        return _proposal_body(request, facts)
    if request.purpose == MessagePurpose.CONFIRM.value:
        category = request.proposed_category or "the category you picked"
        return f"{facts}\n\nDone — set to {category} and approved."
    if request.purpose == MessagePurpose.CLARIFY.value:
        question = request.question or "Which category should this be?"
        return f"{facts}\n\n{question}"
    # fyi / archive_notice / revise_summary / handoff / possibly_inconsistent /
    # diverged_readback — a brief, neutral note (question carries any detail).
    note = request.question or "A quick note on this transaction."
    return f"{facts}\n\n{note}"
