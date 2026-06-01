"""Offline tests for the compose agent (TestModel, no network)."""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.compose import ComposeRequest, compose

_PROPOSAL = ComposeRequest(
    purpose="proposal",
    payee="Amazon",
    amount_display="$54.94",
    txn_date="2026-05-28",
    proposed_category="🛍️ Shopping",
    alternatives=("🍽️ Dining out", "🎁 Gifts"),
    rationale="Looks like a retail purchase.",
)


async def test_compose_returns_the_models_body() -> None:
    body = "Amazon $54.94 — best guess Shopping. Reply to confirm."
    out = await compose(_PROPOSAL, model=TestModel(custom_output_text=body))
    assert out == body


async def test_compose_smoke_with_default_testmodel() -> None:
    # No custom text: TestModel autofills a string; this exercises the wiring
    # (prompt render + run + output extraction) end-to-end, offline.
    out = await compose(_PROPOSAL, model=TestModel())
    assert isinstance(out, str)
    assert out


async def test_compose_prompt_includes_alternatives_and_purpose() -> None:
    # The rendered user prompt must carry the facts the email needs — including
    # the alternative categories the owner should see.
    from ynab_agent.agentic.compose import _format_request

    rendered = _format_request(_PROPOSAL)
    assert "proposal" in rendered
    assert "🛍️ Shopping" in rendered
    assert "🍽️ Dining out" in rendered
    assert "🎁 Gifts" in rendered
