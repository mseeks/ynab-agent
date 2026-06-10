"""Tests for the deterministic proposal/message template (SPEC §5)."""

from __future__ import annotations

from ynab_agent.agentic.compose import (
    ComposeRequest,
    render_autonomy_offer,
    render_body,
    render_body_html,
    render_command_confirm,
    render_offer_accepted,
    render_offer_declined,
    render_receipt_unsupported,
)
from ynab_agent.domain.effects import MessagePurpose


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


def test_receipt_unsupported_is_honest_and_points_to_the_thread() -> None:
    body = render_receipt_unsupported()
    assert "receipts" in body.lower()
    assert "thread" in body.lower()  # points at the path that works


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
