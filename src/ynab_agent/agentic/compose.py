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

from typing import TYPE_CHECKING

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.effects import MessagePurpose

if TYPE_CHECKING:
    from ynab_agent.budget.balance import BalanceOption
    from ynab_agent.domain.money import Money

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


def render_autonomy_offer(payee: str, category: str) -> str:
    """The one-time "want me to auto-handle this payee?" offer body (§14.7 3b).

    A standalone yes/no message (its own thread), so a plain "yes"/"no" reply is
    unambiguous. The owner can also reply with the explicit standing command.
    """
    return (
        f"You've consistently filed {payee} under {category}, and I haven't "
        "had to correct it.\n\n"
        f"Want me to start auto-handling {payee} as {category} from now on? "
        "I'll apply it automatically, flag each one for you, and you can undo "
        "any of them with a one-word reply.\n\n"
        "Reply YES to let me, or NO to keep approving each one yourself."
    )


def render_offer_accepted(payee: str, category: str) -> str:
    """The confirmation sent when the owner accepts the offer (§14.7 3b)."""
    return (
        f"Great — I'll auto-handle {payee} as {category} from now on, and "
        "flag each one so you can see (and undo) it. Reply any time to change "
        "this."
    )


def render_offer_declined(payee: str) -> str:
    """The brief note sent when the owner declines the offer (§14.7 3b)."""
    return (
        f"No problem — I'll keep proposing {payee} for you to approve, same "
        "as before."
    )


def render_balance_options(
    needy_name: str, options: tuple[BalanceOption, ...]
) -> str:
    """The balance offer: ways to cover an overspend, each explained (§8).

    Numbered so the owner can reply "option 2", but a free-text answer ("take it
    from dining instead", "only $50") is read just as well. Each option leads
    with the model's rationale, the plain-English description of the moves.
    """
    lines = [
        f"{needy_name} is over budget. Here are some ways I can cover it by "
        "moving money between categories (nothing leaves your accounts):",
        "",
    ]
    for index, option in enumerate(options, start=1):
        lines.append(f"{index}. {option.label} — {option.rationale}")
    lines += [
        "",
        "Reply with the option you'd like (or your own tweak — e.g. "
        '"option 2 but only $50", or "no thanks").',
    ]
    return "\n".join(lines)


def render_balance_applied(needy_name: str, total: Money) -> str:
    """The confirmation after a coverage plan is applied (SPEC §8)."""
    return (
        f"Done — moved {total} into {needy_name} to cover the overspend. "
        "Reply any time to adjust it."
    )


def render_balance_declined(needy_name: str) -> str:
    """The brief note when the owner declines to cover (SPEC §8)."""
    return (
        f"No problem — I'll leave {needy_name} as is. Reply any time if you "
        "change your mind."
    )


def render_balance_could_not_cover(needy_name: str) -> str:
    """The note when no safe coverage exists from current funds (SPEC §8)."""
    return (
        f"{needy_name} is over budget, but I couldn't find a safe way to cover "
        "it from your current funds. You may want to move money in manually."
    )


def render_balance_failed(needy_name: str, reason: str) -> str:
    """The note when an approved plan can't be applied (SPEC §8)."""
    return (
        f"I couldn't cover {needy_name}: {reason}. Nothing was changed — reply "
        "and we can try another way."
    )


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
    if request.purpose == MessagePurpose.OVERRIDE_NOTICE.value:
        return (
            f"{facts}\n\nNoticed you recategorized this one yourself — I've "
            "backed off auto-handling this payee and will go back to asking."
        )
    # fyi / archive_notice / revise_summary / handoff / possibly_inconsistent /
    # diverged_readback — a brief, neutral note (question carries any detail).
    note = request.question or "A quick note on this transaction."
    return f"{facts}\n\n{note}"
