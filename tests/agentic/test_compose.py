"""Tests for the deterministic proposal/message template (SPEC §5)."""

from __future__ import annotations

from ynab_agent.agentic.compose import ComposeRequest, render_body
from ynab_agent.domain.effects import MessagePurpose


def _req(**kw: object) -> ComposeRequest:
    base: dict[str, object] = {
        "purpose": MessagePurpose.PROPOSAL.value,
        "payee": "Hulu",
        "amount_display": "$-13.07",
        "txn_date": "May 29",
    }
    base.update(kw)
    return ComposeRequest(**base)  # type: ignore[arg-type]


def test_proposal_lays_out_facts_suggestion_and_reply() -> None:
    body = render_body(
        _req(proposed_category="Entertainment", rationale="recurring stream")
    )
    assert "Hulu — $-13.07 — May 29" in body
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
