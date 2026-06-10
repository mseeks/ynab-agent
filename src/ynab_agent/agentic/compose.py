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
from ynab_agent.mail import html as email_html

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
    # Message-specific content carried from the state machine (the model's
    # clarify question, a diverged which-wins comparison) — see the
    # SendThreadMessage effect.
    detail: str | None = None
    # The category NAME a decision actually wrote (confirm / FYI / revision
    # summary / override notice), so those emails name what happened.
    decided_category: str | None = None


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


def render_command_confirm(payee: str, category: str) -> str:
    """The read-back for an explicit "always X as Y" command (SPEC §5c, §0.6).

    A standing command grants autonomy, so the agent echoes its interpretation
    and waits for a one-word confirm before blessing — a command can arrive on a
    brand-new thread where the allow-list is the only gate, so a misread or a
    mistaken send must not silently grant auto-apply.
    """
    return (
        f"You asked me to always categorize {payee} as {category}.\n\n"
        f"Reply YES to confirm — I'll auto-handle {payee} as {category} from "
        "now on, flag each one for you, and you can undo any with a one-word "
        "reply. Reply NO to keep approving each one yourself."
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


def render_receipt_unsupported() -> str:
    """The honest note for a forwarded receipt the join can't process yet (§6).

    The receipt⇄transaction join (W4) is a deferred increment, so rather than
    swallow a forwarded receipt silently, the agent acknowledges it and points
    the owner at the path that does work: replying on the transaction's own
    email thread.
    """
    return (
        "Thanks for forwarding this. I can't match forwarded receipts to "
        "transactions yet, so I haven't filed it.\n\n"
        "To add detail to a specific charge — an item list, a split, or a note "
        "— just reply on that transaction's own email thread and I'll fold it "
        "in."
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
    """The pre-write rejection note: the plan was vetoed, nothing written."""
    return (
        f"I couldn't cover {needy_name}: {reason}. Nothing was changed — reply "
        "and we can try another way."
    )


def render_balance_unverified(needy_name: str) -> str:
    """The post-write failure note (SPEC §8): some moves may have landed.

    Distinct from :func:`render_balance_failed` on purpose — after writes have
    started, "nothing was changed" can be a lie, and an owner who believes it
    won't check. Be honest about the uncertainty and point at YNAB.
    """
    return (
        f"I tried to cover {needy_name} and couldn't confirm every move "
        "landed — some may have applied. Please glance at the affected "
        "categories in YNAB, and reply with what you see; I'll sort out the "
        "rest."
    )


def render_balance_stale(needy_name: str) -> str:
    """The note when approval arrives after the budget month rolled over."""
    return (
        f"The month ended between my offer and your reply, so I didn't move "
        f"anything for {needy_name} — applying it now would change the NEW "
        "month's budget. If it's still over, today's check will raise a fresh "
        "alert."
    )


def _note(request: ComposeRequest) -> str:
    """The message sentence(s) after the facts, per purpose (SPEC §5).

    Shared by the text and HTML renderings so the copy can never drift between
    the two parts. The PROPOSAL purpose is laid out structurally instead and
    never reaches here. Every purpose carries real copy for its lifecycle
    moment (SPEC §3 state notes) — none may fall back to a contentless
    placeholder.
    """
    purpose = request.purpose
    if purpose == MessagePurpose.CONFIRM.value:
        category = (
            request.decided_category
            or request.proposed_category
            or "the category you picked"
        )
        return f"Done — set to {category} and approved."
    if purpose == MessagePurpose.FYI.value:
        filed = (
            f"Filed automatically as {request.decided_category}"
            if request.decided_category
            else "Filed automatically"
        )
        return (
            f"{filed} under your standing rule and flagged in "
            "YNAB so you can spot it. Reply here any time to change it."
        )
    if purpose == MessagePurpose.CLARIFY.value:
        return (
            request.detail
            or request.question
            or "Which category should this be?"
        )
    if purpose == MessagePurpose.REVISE_SUMMARY.value:
        updated = (
            f"Updated — now {request.decided_category}, re-approved."
            if request.decided_category
            else "Updated and re-approved."
        )
        return f"{updated} Reply if that's not right."
    if purpose == MessagePurpose.HANDOFF.value:
        return (
            "I haven't heard back, so I'm leaving this one for "
            "you to handle in YNAB — no more nudges from me. A reply here "
            "still works any time."
        )
    if purpose == MessagePurpose.POSSIBLY_INCONSISTENT.value:
        return (
            "I tried to update this in YNAB and couldn't confirm "
            "the change landed — please check it there, and reply with what "
            "you see so we end up in the right place."
        )
    if purpose == MessagePurpose.DIVERGED_READBACK.value:
        return request.detail or (
            "YNAB now shows something different from what was asked for — "
            "which should win? Reply with your choice and I'll set it."
        )
    if purpose == MessagePurpose.ARCHIVE_NOTICE.value:
        return (
            "This one is still uncategorized and its window is "
            "closing. Reply with a category and I'll file it — or reply "
            "'handled' if you've taken care of it."
        )
    if purpose == MessagePurpose.OVERRIDE_NOTICE.value:
        what = (
            f"you set this to {request.decided_category} yourself"
            if request.decided_category
            else "you recategorized this one yourself"
        )
        return (
            f"Noticed {what} — I've backed off auto-handling this "
            "payee and will go back to asking."
        )
    # An unmapped purpose would mean a new MessagePurpose without copy; say
    # something honest rather than nothing.
    return (
        request.detail
        or request.question
        or ("A quick note on this transaction.")
    )


def render_body(request: ComposeRequest) -> str:
    """Lay out the email body for one transaction message (SPEC §5).

    The subject is templated separately by the caller; this is the plain-text
    part — the canonical copy, of which :func:`render_body_html` is only a
    styled view.
    """
    facts = _facts(request)
    if request.purpose == MessagePurpose.PROPOSAL.value:
        return _proposal_body(request, facts)
    return f"{facts}\n\n{_note(request)}"


# The purposes that ask the owner to decide something: their note is the call
# to action, so the HTML sets it slightly louder than body text.
_PROMPT_PURPOSES = frozenset(
    {
        MessagePurpose.CLARIFY.value,
        MessagePurpose.DIVERGED_READBACK.value,
        MessagePurpose.ARCHIVE_NOTICE.value,
    }
)


def render_body_html(request: ComposeRequest) -> str:
    """The styled rendering of :func:`render_body` — same words, laid out.

    A transaction card (payee, the amount big, date + memo muted), then the
    purpose's content: the proposal's suggested-category box with the reply
    hint under a hairline, a question set slightly louder, or the note as a
    plain paragraph.
    """
    facts = email_html.facts_block(
        payee=request.payee,
        amount=request.amount_display,
        date=request.txn_date,
        memo=request.memo,
    )
    if request.purpose == MessagePurpose.PROPOSAL.value:
        return email_html.wrap_email(
            facts,
            email_html.suggestion_block(
                request.proposed_category or "(needs a category)",
                request.alternatives,
                request.rationale,
            ),
            email_html.footer_block(_REPLY_HINT),
        )
    note = _note(request)
    if request.purpose in _PROMPT_PURPOSES:
        return email_html.wrap_email(facts, email_html.prompt_block(note))
    return email_html.wrap_email(facts, email_html.paragraphs(note))
