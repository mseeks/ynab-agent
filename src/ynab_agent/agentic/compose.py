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
from ynab_agent.domain.money import Money
from ynab_agent.domain.receipt import Receipt, items_brief
from ynab_agent.mail import html as email_html

if TYPE_CHECKING:
    from ynab_agent.budget.balance import (
        BalanceOffer,
        BalanceOption,
        CoordinatedOffer,
        SourceView,
    )

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


def render_offer_unclear(payee: str) -> str:
    """The ack for an unreadable offer/confirm reply (SPEC §14.7 3b).

    The owner spoke; silence back reads as a black hole. Acknowledge, restate
    the question, and name the two words that resolve it.
    """
    return (
        f"Sorry — I couldn't tell from that whether to go ahead. Reply YES "
        f"to let me auto-handle {payee}, or NO to keep approving each one "
        "yourself."
    )


def render_revoked(payee: str) -> str:
    """The confirmation after a "stop auto-handling X" command (SPEC §14.5)."""
    return (
        f"Done — I've stopped auto-handling {payee}. I'll go back to asking "
        "you about each one; if you keep confirming the same category, I may "
        "offer to take it over again later."
    )


def render_revoke_nothing(payee: str) -> str:
    """The honest note when there is no blessed rule to revoke."""
    return (
        f"I'm not auto-handling {payee} right now, so nothing changed. If "
        "you meant a specific charge, reply on that transaction's own email "
        "thread and I'll fix it there."
    )


def render_rules_list(
    blessed: tuple[tuple[str, str], ...],
    eligible: tuple[tuple[str, str], ...],
    observing: int,
) -> str:
    """The "list my rules" reply: the autonomy ladder in plain words (§14).

    ``blessed``/``eligible`` are (payee, category-name) pairs; ``observing``
    is the count of payees still earning consistency.
    """
    lines: list[str] = []
    if blessed:
        lines.append("Auto-handled (you've approved these):")
        lines.extend(f"  - {payee} → {category}" for payee, category in blessed)
    if eligible:
        if lines:
            lines.append("")
        lines.append("Earned trust, awaiting your go-ahead:")
        lines.extend(
            f"  - {payee} → {category}" for payee, category in eligible
        )
    if observing:
        if lines:
            lines.append("")
        lines.append(
            f"Plus {observing} payee{'' if observing == 1 else 's'} I'm "
            "still observing."
        )
    if not lines:
        lines.append(
            "No standing rules yet — I'm still learning your habits from "
            "the categories you confirm."
        )
    lines += [
        "",
        'Say "always categorize X as Y" to add a rule, or "stop '
        'auto-handling X" to revoke one.',
    ]
    return "\n".join(lines)


def render_help() -> str:
    """The capability sheet for a "help" / "what can you do?" message."""
    return (
        "Here's what I do and how to talk to me:\n\n"
        "- I email you each new transaction with a suggested category. Just "
        "reply in your own words — confirm it, name a different category, "
        "or add context (it becomes the memo).\n"
        '- "always categorize X as Y" sets a standing rule (I\'ll read it '
        "back for a YES before it takes effect).\n"
        '- "stop auto-handling X" revokes one, effective immediately.\n'
        '- "list my rules" shows everything I auto-handle or am learning.\n'
        "- Forward a purchase receipt and I'll match it to the charge and "
        "fold the items into its memo (asking when several charges fit).\n"
        "- When a category is over budget I'll email options to cover it — "
        'reply with the option you want (or your own tweak, like "option 2 '
        'but only $50").\n\n'
        "I never change anything without either your reply or a rule you've "
        "explicitly approved, and every automatic move is flagged in YNAB "
        "and undoable with a one-word reply."
    )


def render_receipt_unparseable() -> str:
    """The honest note for a forward the join will not act on (SPEC §6).

    Two cases share it: the extraction said "not a purchase receipt" (a
    shipping notice, a refund, marketing), or it found neither a merchant
    nor a total to match on. The copy covers both without inventing a
    reason, and points at the path that always works.
    """
    return (
        "Thanks for forwarding this — it didn't look like a purchase "
        "receipt I can match (a completed charge with a merchant or "
        "total), so I haven't filed anything.\n\n"
        "To add detail to a specific charge — an item list, a split, or a "
        "note — just reply on that transaction's own email thread and I'll "
        "fold it in."
    )


def render_receipt_ack(summary: str) -> str:
    """The receipt-received acknowledgment (SPEC §6).

    Sent on the forward's own thread once the receipt parses: name what was
    read (so a misread is visible immediately) and what happens next —
    promising only what every match path actually does (the detail surfaces
    on the charge's side; settled charges get it folded into the memo).
    """
    return (
        f"Got it — I read this as: {summary}.\n\n"
        "I'll match it to the right transaction and bring the detail to "
        "that charge. If no match has posted yet, I'll keep checking as "
        "new transactions come in."
    )


def render_receipt_matched(summary: str, charge: str) -> str:
    """The settled-charge confirmation: matched and memo'd (SPEC §6).

    Sent on the receipt's own thread when the matched charge has no live
    conversation of its own (hand-approved, pre-install, long archived) —
    the confirmable statement §6 requires, naming both sides.
    """
    return (
        f"Matched your receipt ({summary}) to {charge} and added the "
        "items to its memo. Nothing else on the charge was touched — "
        "reply if that match looks wrong."
    )


# The disambiguation instruction, shared verbatim by the text and HTML
# renderings so the honesty guarantee (threads only promised when one
# exists) cannot drift between the two parts.
_DISAMBIGUATION_WITH_THREADS = (
    "Where a charge has its own email thread from me, reply there "
    "with the detail you want filed. For any that don't, add the "
    "note directly in YNAB — I won't guess between them."
)
_DISAMBIGUATION_NO_THREADS = (
    "None of these are in my email queue (they're already settled "
    "in YNAB), so I won't guess between them — add the detail to "
    "the right charge's memo directly in YNAB."
)


def render_receipt_disambiguation(
    summary: str, options: tuple[str, ...], *, with_threads: bool
) -> str:
    """The which-charge-is-this question, asked exactly once (SPEC §6).

    ``with_threads`` keeps the instruction honest: the charges' own email
    threads are only promised when at least one exists (a hand-approved or
    pre-install charge was never triaged and has none).
    """
    lines = [
        f"Your receipt ({summary}) plausibly matches more than one charge:",
        "",
    ]
    lines.extend(
        f"{index}. {option}" for index, option in enumerate(options, start=1)
    )
    instruction = (
        _DISAMBIGUATION_WITH_THREADS
        if with_threads
        else _DISAMBIGUATION_NO_THREADS
    )
    lines += ["", instruction]
    return "\n".join(lines)


def render_receipt_no_match(summary: str) -> str:
    """The aged-out closure note: the join stopped trying (SPEC §6).

    Worded for both expiry paths — never matched at all, or asked once and
    never resolved — so it cannot contradict an earlier disambiguation
    email.
    """
    return (
        f"I couldn't pin your receipt ({summary}) to a single charge "
        "within 30 days, so I've stopped trying.\n\n"
        "If you want the detail kept, add it to the charge's memo in YNAB "
        "— or forward the receipt again and I'll take another look."
    )


def _receipt_card(receipt: Receipt) -> str:
    """The receipt as a card: merchant, the total big, date + items muted.

    The same shape as the transaction card (``facts_block``) — a receipt's
    merchant/total/date/items map straight onto payee/amount/date/memo — so
    the receipt emails read as kin to the proposal emails.
    """
    return email_html.facts_block(
        payee=receipt.merchant or "Forwarded receipt",
        amount=str(receipt.total) if receipt.total is not None else "",
        date=receipt.date.isoformat() if receipt.date is not None else "",
        memo=items_brief(receipt),
    )


def render_receipt_ack_html(receipt: Receipt) -> str:
    """The styled receipt ack: what was read as a card, then the plan."""
    return email_html.wrap_email(
        _receipt_card(receipt),
        email_html.paragraphs(
            "Got it — this is what I read.\n\n"
            "I'll match it to the right transaction and bring the detail "
            "to that charge. If no match has posted yet, I'll keep "
            "checking as new transactions come in."
        ),
    )


def render_receipt_matched_html(receipt: Receipt, charge: str) -> str:
    """The styled matched confirmation: the card, then both sides named."""
    return email_html.wrap_email(
        _receipt_card(receipt),
        email_html.paragraphs(
            f"Matched to {charge} and added the items to its memo. "
            "Nothing else on the charge was touched — reply if that "
            "match looks wrong."
        ),
    )


def render_receipt_disambiguation_html(
    receipt: Receipt, options: tuple[str, ...], *, with_threads: bool
) -> str:
    """The styled which-charge question: card, options, honest instruction."""
    numbered = "\n".join(
        f"{index}. {option}" for index, option in enumerate(options, start=1)
    )
    instruction = (
        _DISAMBIGUATION_WITH_THREADS
        if with_threads
        else _DISAMBIGUATION_NO_THREADS
    )
    return email_html.wrap_email(
        _receipt_card(receipt),
        email_html.prompt_block("This plausibly matches more than one charge:"),
        email_html.paragraphs(numbered),
        email_html.paragraphs(instruction),
    )


def render_receipt_no_match_html(receipt: Receipt) -> str:
    """The styled aged-out closure: the card, then where the detail can go."""
    return email_html.wrap_email(
        _receipt_card(receipt),
        email_html.paragraphs(
            "I couldn't pin this receipt to a single charge within 30 "
            "days, so I've stopped trying.\n\n"
            "If you want the detail kept, add it to the charge's memo in "
            "YNAB — or forward the receipt again and I'll take another look."
        ),
    )


def _leaves_line(
    option: BalanceOption, views: dict[str, SourceView]
) -> str | None:
    """The 'what this leaves' summary: each donor's slack after the moves.

    e.g. ``Leaves Ready to Assign at $130, Vacation with $260 to spare.`` —
    so the owner sees no category is being drained dry.
    """
    pulled: dict[str, Money] = {}
    for move in option.moves:
        source = str(move.source)
        pulled[source] = pulled.get(source, Money.zero()) + move.amount
    parts = []
    for source, amount in pulled.items():
        view = views.get(source)
        if view is None:
            continue
        left = view.slack - amount
        parts.append(f"{view.name} with {left} still to spare")
    if not parts:
        return None
    return "Leaves " + ", ".join(parts) + "."


def render_balance_options(needy_name: str, offer: BalanceOffer) -> str:
    """The balance offer: ways to cover an overspend, with real numbers (§8).

    Numbered so the owner can reply "option 2", but a free-text answer ("take it
    from dining instead", "only $50") is read just as well. Each option leads
    with the model's rationale, then lists its moves with the amount, the
    donor's name, and what the move leaves it ("~$430 still to spare"), plus a
    summary of what the whole plan leaves — so the owner approves real money on
    real numbers, not prose alone.
    """
    views = {str(view.category): view for view in offer.sources}
    lines = [
        f"{needy_name} is over budget. Here are some ways I can cover it by "
        "moving money between categories (nothing leaves your accounts):",
        "",
    ]
    for index, option in enumerate(offer.options, start=1):
        lines.append(f"{index}. {option.label} — {option.rationale}")
        # Track each donor's remaining slack across this option's moves, so two
        # pulls from one source show a consistent running figure (not each
        # ignoring the other), matching the summary line.
        left = {str(view.category): view.slack for view in offer.sources}
        for move in option.moves:
            source = str(move.source)
            view = views.get(source)
            name = view.name if view is not None else source
            detail = f"   {move.amount} from {name}"
            if source in left:
                left[source] = left[source] - move.amount
                detail += f", ~{left[source]} still to spare"
            lines.append(detail)
        leaves = _leaves_line(option, views)
        if leaves is not None:
            lines.append(f"   {leaves}")
    lines += [
        "",
        "Reply with the option you'd like (or your own tweak — e.g. "
        '"option 2 but only $50", or "no thanks").',
    ]
    return "\n".join(lines)


def render_coordinated_offer(offer: CoordinatedOffer) -> str:
    """One coordinated coverage plan for a whole monitor pass (SPEC §8, #46).

    Lists each move (amount, the category it funds, the donor), then what the
    plan leaves each donor, and any category the shared pool couldn't reach — so
    the owner approves the whole plan on real numbers. The reply is whole-plan:
    "do it" applies it, "no thanks" declines.
    """
    covered = len({line.destination for line in offer.lines})
    total_cats = covered + len(offer.uncovered)
    plural = "category" if total_cats == 1 else "categories"
    lines = [
        f"{total_cats} {plural} over or trending this month — here is one plan "
        f"to cover {offer.total} by moving money between categories (nothing "
        "leaves your accounts):",
        "",
    ]
    lines.extend(
        f"  {line.amount} -> {line.destination} from {line.source}"
        for line in offer.lines
    )
    pulled: dict[str, Money] = {}
    for line in offer.lines:
        pulled[line.source] = (
            pulled.get(line.source, Money.zero()) + line.amount
        )
    slack_by_name = {view.name: view.slack for view in offer.sources}
    leaves = [
        f"{source} with {slack_by_name[source] - amount} still to spare"
        for source, amount in pulled.items()
        if source in slack_by_name
    ]
    if leaves:
        lines += ["", "Leaves " + ", ".join(leaves) + ". No category drained."]
    if offer.uncovered:
        lines += [
            "",
            "I couldn't fully cover "
            + ", ".join(offer.uncovered)
            + " — the categories with room are all heading over themselves.",
        ]
    lines += ["", 'Reply "do it" to apply the whole plan, or "no thanks".']
    return "\n".join(lines)


def render_balance_over_cap(moves: int, cap: int) -> str:
    """The note when a coordinated plan exceeds the daily move cap (§8, #46).

    The hard floor caps how many budget moves the agent applies in a day; a plan
    over it isn't applied (the owner can split it), so the note is honest that
    nothing changed.
    """
    return (
        f"This plan needs {moves} separate moves, more than the {cap} I'll "
        "make in a single day as a safety limit. I haven't changed anything — "
        "reply and we can cover the most urgent categories first, or move the "
        "money in YNAB yourself."
    )


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
    """The note when no safe coverage exists from current funds (SPEC §8).

    Explains *why* in plain language: the categories with room are themselves
    heading over, so pulling from them would just move the problem. Slack-based
    donor exclusion makes this case more common (and correct), and the
    explanation is what keeps it trustworthy.
    """
    return (
        f"{needy_name} is over budget, but I couldn't find a safe way to cover "
        "it: the categories with room to spare are all heading over "
        "themselves, so pulling from them would just move the shortfall "
        "around. You may want "
        "to move money in manually, or trim a category that's genuinely flush."
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
