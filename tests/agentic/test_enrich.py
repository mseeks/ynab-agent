"""Tests for the enrichment agent (SPEC §4.1, §0.5).

Offline tests drive a ``TestModel`` (no network, deterministic) to exercise the
agent wiring and the domain mapping. One opt-in live test runs the real Ollama
Gemma model — SPEC §0.5 spike #2 — and is skipped unless
``YNAB_AGENT_LIVE_OLLAMA`` is set, so the gate stays fast and offline.
"""

from __future__ import annotations

import os

import pytest
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.enrich import (
    CandidateCategory,
    EnrichmentRequest,
    EnrichmentSuggestion,
    propose,
    to_proposal,
)
from ynab_agent.agentic.model import build_model
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.enums import Confidence, SourceKind

_REQUEST = EnrichmentRequest(
    payee="Blue Bottle Coffee",
    amount_display="-$4.50",
    candidates=(
        CandidateCategory(id="dining", name="Dining Out"),
        CandidateCategory(id="groceries", name="Groceries"),
    ),
)


async def test_propose_returns_the_models_structured_suggestion() -> None:
    model = TestModel(
        custom_output_args={
            "category_id": "dining",
            "confidence": "high",
            "rationale": "a coffee shop",
        }
    )
    out = await propose(_REQUEST, model=model)
    assert isinstance(out, EnrichmentSuggestion)
    assert out.category_id == "dining"
    assert out.confidence is Confidence.HIGH
    assert out.rationale == "a coffee shop"


async def test_propose_wiring_smoke_with_default_testmodel() -> None:
    # No custom output → TestModel autofills a valid suggestion; this just
    # proves the agent/output-schema wiring round-trips.
    out = await propose(_REQUEST, model=TestModel())
    assert isinstance(out, EnrichmentSuggestion)


def test_to_proposal_maps_onto_the_domain_proposal() -> None:
    suggestion = EnrichmentSuggestion(
        category_id="dining",
        confidence=Confidence.MEDIUM,
        rationale="coffee",
    )
    proposal = to_proposal(suggestion)
    assert isinstance(proposal.allocation, ProposedCategory)
    assert proposal.allocation.category == "dining"
    assert proposal.confidence is Confidence.MEDIUM
    assert proposal.sources[0].kind is SourceKind.MODEL


def test_build_model_constructs_an_ollama_model() -> None:
    assert isinstance(build_model(model_name="gemma4:e4b"), Model)


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_OLLAMA"),
    reason="set YNAB_AGENT_LIVE_OLLAMA=1 to run the live Gemma smoke",
)
async def test_live_gemma_categorizes_a_coffee_shop() -> None:
    # SPEC §0.5 spike #2: real Gemma over Ollama returns usable content.
    out = await propose(_REQUEST)
    assert out.category_id in {"dining", "groceries"}
    assert out.rationale
