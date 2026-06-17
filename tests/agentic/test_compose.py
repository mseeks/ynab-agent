"""Tests for the deterministic proposal/message template (SPEC §5)."""

from __future__ import annotations

from ynab_agent.agentic.compose import (
    ComposeRequest,
    render_autonomy_offer,
    render_balance_could_not_cover,
    render_balance_options,
    render_body,
    render_body_html,
    render_command_confirm,
    render_offer_accepted,
    render_offer_declined,
    render_receipt_ack,
    render_receipt_disambiguation,
    render_receipt_matched,
    render_receipt_no_match,
    render_receipt_unparseable,
)
from ynab_agent.budget.balance import (
    BalanceOffer,
    BalanceOption,
    BudgetMove,
    SourceView,
)
from ynab_agent.domain.effects import MessagePurpose
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money


def _req(**kw: object) -> ComposeRequest:
    base: dict[str, object] = {
        "purpose": MessagePurpose.PROPOSAL.value,
        "payee": "Hulu",
        "amount_display": "-$13.07",
        "txn_date": "May 29",
    }
    base.update(kw)
    return ComposeRequest(**base)  # type: ignore[arg-type]


def test_proposal_lays_out_facts_suggestion_and_reply() -> None:
    body = render_body(
        _req(proposed_category="Entertainment", rationale="recurring stream")
    )
    assert "Hulu — -$13.07 — May 29" in body
    assert "Suggested: Entertainment" in body
    assert "recurring stream" in body
    assert "reply" in body.lower()
    assert "[YNAB]" not in body


def test_proposal_puts_alternatives_on_the_suggested_line() -> None:
    body = render_body(
        _req(
            proposed_category="Entertainment",
            alternatives=("Streaming", "Fun Money"),
        )
    )
    suggested = next(
        line for line in body.splitlines() if line.startswith("Suggested:")
    )
    assert "Streaming" in suggested and "Fun Money" in suggested


def test_proposal_shows_the_memo_when_present() -> None:
    # Amazon-style item detail rides in the memo.
    body = render_body(
        _req(proposed_category="Shopping", memo="USB-C cable, phone stand")
    )
    assert "USB-C cable, phone stand" in body


def test_proposal_omits_memo_block_when_absent() -> None:
    body = render_body(_req(proposed_category="Shopping"))
    # The header line stands alone; nothing between it and the blank line.
    assert body.splitlines()[1] == ""


def test_confirm_is_brief_and_names_the_category() -> None:
    body = render_body(
        _req(purpose=MessagePurpose.CONFIRM.value, proposed_category="Dining")
    )
    assert "approved" in body.lower()
    assert "Dining" in body


def test_clarify_asks_the_question() -> None:
    body = render_body(
        _req(purpose=MessagePurpose.CLARIFY.value, question="Which trip?")
    )
    assert "Which trip?" in body


def test_clarify_prefers_the_carried_detail() -> None:
    # The model's actual question rides the effect as `detail` (SPEC §3, §5).
    body = render_body(
        _req(
            purpose=MessagePurpose.CLARIFY.value,
            detail="Was this the annual renewal?",
        )
    )
    assert "Was this the annual renewal?" in body


def test_confirm_names_the_decided_category() -> None:
    body = render_body(
        _req(purpose=MessagePurpose.CONFIRM.value, decided_category="Groceries")
    )
    assert "Groceries" in body
    assert "the category you picked" not in body


def test_fyi_names_category_rule_and_undo() -> None:
    body = render_body(
        _req(purpose=MessagePurpose.FYI.value, decided_category="Dining")
    )
    assert "Dining" in body
    assert "standing rule" in body
    assert "Reply" in body  # the one-reply undo promise (SPEC §14.5)


def test_revise_summary_says_what_changed() -> None:
    body = render_body(
        _req(
            purpose=MessagePurpose.REVISE_SUMMARY.value,
            decided_category="Gifts",
        )
    )
    assert "Gifts" in body
    assert "Updated" in body


def test_handoff_says_its_yours_now() -> None:
    body = render_body(_req(purpose=MessagePurpose.HANDOFF.value))
    assert "YNAB" in body
    assert "haven't heard back" in body.lower()
    assert "A quick note" not in body


def test_possibly_inconsistent_says_check_ynab() -> None:
    body = render_body(_req(purpose=MessagePurpose.POSSIBLY_INCONSISTENT.value))
    assert "couldn't confirm" in body.lower()
    assert "YNAB" in body


def test_diverged_readback_uses_the_carried_comparison() -> None:
    body = render_body(
        _req(
            purpose=MessagePurpose.DIVERGED_READBACK.value,
            detail="YNAB now shows Groceries, but your reply asked for Gifts "
            "— which should win?",
        )
    )
    assert "Groceries" in body
    assert "which should win?" in body


def test_archive_notice_asks_for_a_category_or_handled() -> None:
    body = render_body(_req(purpose=MessagePurpose.ARCHIVE_NOTICE.value))
    assert "uncategorized" in body.lower()
    assert "handled" in body.lower()


def test_override_notice_names_what_the_owner_set() -> None:
    body = render_body(
        _req(
            purpose=MessagePurpose.OVERRIDE_NOTICE.value,
            decided_category="Travel",
        )
    )
    assert "Travel" in body
    assert "backed off" in body


def test_no_purpose_renders_the_placeholder_any_more() -> None:
    # Every real lifecycle purpose has copy; the placeholder only survives as
    # the never-reached fallback for an unmapped future purpose.
    for purpose in MessagePurpose:
        body = render_body(_req(purpose=purpose.value))
        assert "A quick note on this transaction." not in body, purpose


def test_html_proposal_carries_the_same_facts_and_suggestion() -> None:
    html = render_body_html(
        _req(
            proposed_category="Entertainment",
            alternatives=("Streaming",),
            rationale="recurring stream",
            memo="monthly plan",
        )
    )
    assert "Hulu" in html
    assert "-$13.07" in html
    assert "May 29" in html
    assert "monthly plan" in html
    assert "Entertainment" in html
    assert "or: Streaming" in html
    assert "recurring stream" in html
    assert "reply" in html.lower()


def test_html_escapes_model_and_ynab_text() -> None:
    html = render_body_html(
        _req(
            payee="Joe's <Diner> & Co",
            proposed_category="Dining",
            rationale='looks like a "restaurant"',
        )
    )
    assert "<Diner>" not in html
    assert "Joe&#x27;s &lt;Diner&gt; &amp; Co" in html


def test_html_clarify_sets_the_question_as_the_prompt() -> None:
    html = render_body_html(
        _req(purpose=MessagePurpose.CLARIFY.value, question="Which trip?")
    )
    assert "Which trip?" in html
    assert "font-weight:500" in html  # the call to action is set louder


def test_every_purpose_renders_html_with_the_same_words() -> None:
    # The HTML is a styled view of the text part, never different copy: every
    # purpose's note must appear (escaped) in the HTML rendering too. The
    # proposal is laid out structurally and covered by its own test above.
    from ynab_agent.mail.html import escape

    for purpose in MessagePurpose:
        if purpose is MessagePurpose.PROPOSAL:
            continue
        request = _req(
            purpose=purpose.value,
            proposed_category="Dining",
            decided_category="Dining",
            detail="the carried detail",
        )
        text = render_body(request)
        html = render_body_html(request)
        note = text.split("\n\n", 1)[1]
        for line in note.splitlines():
            assert escape(line) in html or not line, (purpose, line)


def test_autonomy_offer_names_payee_category_and_asks_yes_no() -> None:
    body = render_autonomy_offer("Spotify", "Subscriptions")
    assert "Spotify" in body
    assert "Subscriptions" in body
    assert "YES" in body and "NO" in body


def test_offer_accepted_confirms_auto_handling() -> None:
    body = render_offer_accepted("Spotify", "Subscriptions")
    assert "Spotify" in body
    assert "Subscriptions" in body


def test_offer_declined_says_it_keeps_proposing() -> None:
    body = render_offer_declined("Spotify")
    assert "Spotify" in body
    assert "propos" in body.lower()


def test_command_confirm_echoes_the_command_and_asks_for_yes() -> None:
    body = render_command_confirm("Costco", "Groceries")
    assert "Costco" in body
    assert "Groceries" in body
    assert "YES" in body  # an explicit one-word confirm (SPEC §0.6)


def test_receipt_unparseable_is_honest_and_points_to_the_thread() -> None:
    body = render_receipt_unparseable()
    assert "didn't look like a purchase receipt" in body
    assert "thread" in body.lower()  # points at the path that works


def test_receipt_ack_names_what_was_read() -> None:
    # Naming the extraction makes a misread visible immediately, and the
    # promise covers only what every match path actually does.
    body = render_receipt_ack("Whole Foods — $23.48 (Corn Starch)")
    assert "Whole Foods — $23.48 (Corn Starch)" in body
    assert "bring the detail to" in body


def test_receipt_matched_names_both_sides() -> None:
    body = render_receipt_matched(
        "Whole Foods — $23.48", "Whole Foods — -$23.48 on Jun 8"
    )
    assert "Whole Foods — $23.48" in body
    assert "Whole Foods — -$23.48 on Jun 8" in body
    assert "Nothing else on the charge was touched" in body


def test_receipt_disambiguation_lists_numbered_options() -> None:
    body = render_receipt_disambiguation(
        "Blue Bottle — $4.50",
        (
            "-$4.50 at Blue Bottle Coffee on May 28",
            "-$4.50 at Blue Bottle Coffee on May 29",
        ),
        with_threads=True,
    )
    assert "1. -$4.50 at Blue Bottle Coffee on May 28" in body
    assert "2. -$4.50 at Blue Bottle Coffee on May 29" in body
    assert "reply there" in body.lower()


def test_receipt_disambiguation_never_promises_missing_threads() -> None:
    # Hand-approved / pre-install candidates have no email threads; the
    # instruction must not point at threads that do not exist.
    body = render_receipt_disambiguation(
        "Blue Bottle — $4.50",
        (
            "-$4.50 at Blue Bottle Coffee on May 28",
            "-$4.50 at Blue Bottle Coffee on May 29",
        ),
        with_threads=False,
    )
    assert "email thread from me" not in body
    assert "directly in YNAB" in body


def test_receipt_no_match_explains_the_age_out() -> None:
    # Worded for both expiry paths (never matched / asked but unresolved),
    # so it cannot contradict an earlier disambiguation email.
    body = render_receipt_no_match("Costco — $80.00")
    assert "Costco — $80.00" in body
    assert "30 days" in body
    assert "couldn't pin" in body
    assert "forward the receipt again" in body


def test_offer_unclear_restates_the_yes_no_question() -> None:
    from ynab_agent.agentic.compose import render_offer_unclear

    body = render_offer_unclear("Spotify")
    assert "Spotify" in body
    assert "YES" in body and "NO" in body


def test_revoked_confirms_and_explains_what_changes() -> None:
    from ynab_agent.agentic.compose import render_revoked

    body = render_revoked("Costco")
    assert "stopped auto-handling Costco" in body
    assert "asking" in body


def test_revoke_nothing_is_honest_and_redirects() -> None:
    from ynab_agent.agentic.compose import render_revoke_nothing

    body = render_revoke_nothing("Costco")
    assert "not auto-handling Costco" in body
    assert "nothing changed" in body


def test_rules_list_names_each_tier_and_the_verbs() -> None:
    from ynab_agent.agentic.compose import render_rules_list

    body = render_rules_list(
        blessed=(("Spotify", "Subscriptions"),),
        eligible=(("Costco", "Groceries"),),
        observing=3,
    )
    assert "Spotify → Subscriptions" in body
    assert "Costco → Groceries" in body
    assert "3 payees" in body
    assert "stop auto-handling" in body  # teaches the undo verb


def test_rules_list_empty_is_friendly() -> None:
    from ynab_agent.agentic.compose import render_rules_list

    body = render_rules_list(blessed=(), eligible=(), observing=0)
    assert "No standing rules yet" in body


def test_help_covers_the_verbs_and_the_safety_promise() -> None:
    from ynab_agent.agentic.compose import render_help

    body = render_help()
    assert "always categorize" in body
    assert "stop auto-handling" in body
    assert "list my rules" in body
    assert "never change anything" in body


def _parsed_receipt() -> object:
    import datetime

    from ynab_agent.domain.ids import ReceiptId
    from ynab_agent.domain.money import Money
    from ynab_agent.domain.receipt import Receipt, ReceiptLineItem

    return Receipt(
        id=ReceiptId("r1"),
        parked_at=datetime.datetime(2026, 6, 10, 12, 0, tzinfo=datetime.UTC),
        merchant="Apple",
        date=datetime.date(2026, 6, 7),
        total=Money(milliunits=-9990),
        line_items=(ReceiptLineItem(description="iCloud+ with 2 TB"),),
    )


def test_receipt_html_card_carries_the_parsed_facts() -> None:
    # The receipt emails get the same card treatment the transaction emails
    # got (merchant/total/date/items ↔ payee/amount/date/memo) — they used
    # to fall back to the generic typography and read as unstyled text.
    from ynab_agent.agentic.compose import render_receipt_ack_html

    html = render_receipt_ack_html(_parsed_receipt())  # type: ignore[arg-type]
    assert "Apple" in html
    assert "2026-06-07" in html
    assert "iCloud+ with 2 TB" in html
    assert "bring the detail" in html


def test_receipt_disambiguation_html_keeps_the_honest_instruction() -> None:
    from ynab_agent.agentic.compose import render_receipt_disambiguation_html

    options = (
        "-$4.50 at Blue Bottle Coffee on May 28",
        "-$4.50 at Blue Bottle Coffee on May 29",
    )
    with_threads = render_receipt_disambiguation_html(
        _parsed_receipt(),  # type: ignore[arg-type]
        options,
        with_threads=True,
    )
    without = render_receipt_disambiguation_html(
        _parsed_receipt(),  # type: ignore[arg-type]
        options,
        with_threads=False,
    )
    assert "May 28" in with_threads and "May 29" in with_threads
    assert "reply there" in with_threads
    assert "email thread from me" not in without
    assert "directly in YNAB" in without


def test_receipt_matched_and_no_match_html_render_the_card() -> None:
    from ynab_agent.agentic.compose import (
        render_receipt_matched_html,
        render_receipt_no_match_html,
    )

    matched = render_receipt_matched_html(
        _parsed_receipt(),  # type: ignore[arg-type]
        "Apple — -$9.99 on Jun 7",
    )
    assert "Apple — -$9.99 on Jun 7" in matched
    assert "Nothing else on the charge was touched" in matched
    no_match = render_receipt_no_match_html(_parsed_receipt())  # type: ignore[arg-type]
    assert "30" in no_match
    assert "forward the receipt again" in no_match


def test_render_balance_options_shows_amounts_donors_and_after_state() -> None:
    offer = BalanceOffer(
        options=(
            BalanceOption(
                label="From Vacation",
                moves=(
                    BudgetMove(
                        source=CategoryId("vacation"),
                        destination=CategoryId("dining"),
                        amount=Money.from_currency("170"),
                    ),
                ),
                rationale="Vacation has plenty to spare.",
            ),
        ),
        sources=(
            SourceView(
                category=CategoryId("vacation"),
                name="Vacation",
                slack=Money.from_currency("600"),
            ),
        ),
    )
    body = render_balance_options("Dining", offer)
    assert "$170.00 from Vacation" in body  # amount + donor name
    assert "$430.00 still to spare" in body  # after-state per move (600 - 170)
    assert "Leaves Vacation" in body  # the summary "leaves" line
    assert "no thanks" in body  # the reply hint is preserved


def test_render_balance_could_not_cover_explains_why() -> None:
    body = render_balance_could_not_cover("Dining")
    assert "heading over themselves" in body  # the plain-language why


def test_render_balance_options_running_slack_for_two_pulls() -> None:
    # Two moves from one $300-slack source show a running remainder
    # (300 - 60 = 240, then 240 - 50 = 190), not each ignoring the other.
    offer = BalanceOffer(
        options=(
            BalanceOption(
                label="Twice from Vacation",
                moves=(
                    BudgetMove(
                        source=CategoryId("vacation"),
                        destination=CategoryId("dining"),
                        amount=Money.from_currency("60"),
                    ),
                    BudgetMove(
                        source=CategoryId("vacation"),
                        destination=CategoryId("dining"),
                        amount=Money.from_currency("50"),
                    ),
                ),
                rationale="Vacation can cover it in two slices.",
            ),
        ),
        sources=(
            SourceView(
                category=CategoryId("vacation"),
                name="Vacation",
                slack=Money.from_currency("300"),
            ),
        ),
    )
    body = render_balance_options("Dining", offer)
    assert "$240.00 still to spare" in body
    assert "$190.00 still to spare" in body
    assert "Leaves Vacation with $190.00 still to spare" in body
