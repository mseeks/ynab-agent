"""Tests for the minimalist email HTML layer (SPEC §5)."""

from __future__ import annotations

from ynab_agent.mail.html import (
    facts_block,
    footer_block,
    prompt_block,
    suggestion_block,
    text_to_html,
    wrap_email,
)


def test_text_to_html_wraps_paragraphs() -> None:
    html = text_to_html("First thought.\n\nSecond thought.")
    assert html.count("<p ") == 2
    assert "First thought." in html
    assert "Second thought." in html
    assert "font-family" in html  # the shared shell sets the type


def test_text_to_html_escapes_content() -> None:
    html = text_to_html("a <script>alert('x')</script> & more")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; more" in html


def test_text_to_html_keeps_single_newlines_as_breaks() -> None:
    html = text_to_html("line one\nline two")
    assert "line one<br>line two" in html


def test_numbered_blocks_become_styled_option_rows() -> None:
    # The balance offer's shape: "N. label — rationale" lines. The label is
    # bolded so the choices scan; the rationale sits muted beneath.
    body = (
        "Dining is over budget. Ways to cover it:\n\n"
        "1. Move $20 from Groceries — it has the most slack\n"
        "2. Move $20 from Fun Money — lightly used this month\n\n"
        "Reply with the option you'd like."
    )
    html = text_to_html(body)
    assert "Move $20 from Groceries" in html
    assert "it has the most slack" in html
    assert html.count("font-weight:600") >= 2  # each label bolded
    assert "1." in html
    assert "2." in html


def test_facts_block_shows_payee_amount_date_and_memo() -> None:
    html = facts_block(
        payee="Blue Bottle <Coffee>",
        amount="-$4.50",
        date="May 29",
        memo="oat latte",
    )
    assert "Blue Bottle &lt;Coffee&gt;" in html  # escaped
    assert "-$4.50" in html
    assert "May 29" in html
    assert "oat latte" in html


def test_facts_block_omits_a_missing_memo() -> None:
    html = facts_block(payee="Hulu", amount="-$13.07", date="May 29")
    assert html.count("margin-top:2px") == 0  # no memo line rendered


def test_suggestion_block_names_category_alternatives_rationale() -> None:
    html = suggestion_block(
        "Entertainment", ("Streaming", "Fun Money"), "recurring subscription"
    )
    assert "Suggested" in html
    assert "Entertainment" in html
    assert "or: Streaming, Fun Money" in html
    assert "recurring subscription" in html


def test_blocks_compose_into_the_shell() -> None:
    html = wrap_email(
        facts_block(payee="Hulu", amount="-$13.07", date="May 29"),
        prompt_block("Which category should this be?"),
        footer_block("Just reply in your own words."),
    )
    assert html.startswith("<div")
    assert "Which category should this be?" in html
    assert "Just reply in your own words." in html
