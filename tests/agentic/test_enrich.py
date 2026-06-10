"""Tests for the enrichment agent (SPEC §4.1, §0.5).

Offline tests drive a ``TestModel`` (no network, deterministic) to exercise the
agent wiring and the domain mapping. One opt-in live test runs the real Ollama
Gemma model — SPEC §0.5 spike #2 — and is skipped unless
``YNAB_AGENT_LIVE_OLLAMA`` is set, so the gate stays fast and offline.
"""

from __future__ import annotations

import datetime
import os

import pytest
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.enrich import (
    CandidateCategory,
    EnrichmentRequest,
    EnrichmentSuggestion,
    decide_enrichment,
    propose,
    review_auto_apply,
    to_proposal,
    validate_suggestion,
)
from ynab_agent.agentic.model import build_model
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.enums import (
    Confidence,
    ReviewVerdict,
    RuleSource,
    SourceKind,
    TrustState,
)
from ynab_agent.domain.events import AskHuman, AutoApply
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    RuleId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.floor import AutoActionCounters

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


def test_to_proposal_carries_alternatives_filtering_the_chosen() -> None:
    suggestion = EnrichmentSuggestion(
        category_id="dining",
        confidence=Confidence.LOW,
        rationale="maybe",
        # the chosen id and an empty entry are filtered out defensively
        alternatives=("coffee", "dining", "", "groceries"),
    )
    proposal = to_proposal(suggestion)
    assert [str(a) for a in proposal.alternatives] == ["coffee", "groceries"]


def test_build_model_constructs_an_ollama_model() -> None:
    assert isinstance(build_model(model_name="gemma4:e4b"), Model)


def test_validate_suggestion_rejects_a_hallucinated_id() -> None:
    # An invented id would surface as a raw id in the email subject and 400 on
    # write — raising lets the activity retry with a fresh model run.
    bogus = EnrichmentSuggestion(
        category_id="10683d916894", confidence=Confidence.HIGH, rationale="x"
    )
    with pytest.raises(ValueError, match="unknown category id"):
        validate_suggestion(bogus, _REQUEST.candidates)


def test_validate_suggestion_drops_hallucinated_alternatives() -> None:
    # Alternatives are framing only — invalid ones are dropped, not fatal.
    mixed = EnrichmentSuggestion(
        category_id="dining",
        confidence=Confidence.HIGH,
        rationale="x",
        alternatives=("groceries", "10683d916894"),
    )
    assert validate_suggestion(mixed, _REQUEST.candidates).alternatives == (
        "groceries",
    )


def test_review_auto_apply_proceeds_when_blessed_is_plausible() -> None:
    top = EnrichmentSuggestion(
        category_id="dining", confidence=Confidence.HIGH, rationale="x"
    )
    assert review_auto_apply("dining", top) is ReviewVerdict.PROCEED
    alt = EnrichmentSuggestion(
        category_id="groceries",
        confidence=Confidence.HIGH,
        rationale="x",
        alternatives=("dining",),
    )
    assert review_auto_apply("dining", alt) is ReviewVerdict.PROCEED


def test_review_auto_apply_escalates_when_blessed_is_implausible() -> None:
    other = EnrichmentSuggestion(
        category_id="groceries",
        confidence=Confidence.HIGH,
        rationale="x",
        alternatives=("software",),
    )
    assert review_auto_apply("dining", other) is ReviewVerdict.ESCALATE_TO_HUMAN


_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)


def _snapshot() -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId("t1"),
        account=AccountId("a1"),
        payee="Blue Bottle Coffee",
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, 28),
    )


def _blessed_rule() -> Rule:
    return Rule(
        id=RuleId("r1"),
        match=RuleMatch(payee_pattern="Blue Bottle"),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("dining"))
        ),
        trust=TrustState.TRUSTED,
        source=RuleSource.HUMAN_EXPLICIT,
    )


def _judge(category_id: str, alternatives: tuple[str, ...] = ()) -> TestModel:
    """A TestModel whose suggestion stands in for the review's judgment."""
    return TestModel(
        custom_output_args={
            "category_id": category_id,
            "confidence": "high",
            "rationale": "independent read",
            "alternatives": list(alternatives),
        }
    )


async def test_decide_enrichment_auto_applies_when_the_review_agrees() -> None:
    # The independent review picks the same category the blessed rule would →
    # the auto-apply proceeds (SPEC §0.6 Layer 2).
    outcome = await decide_enrichment(
        _snapshot(),
        _REQUEST.candidates,
        [_blessed_rule()],
        AutoActionCounters(),
        now=_NOW,
        model=_judge("dining"),
    )
    assert isinstance(outcome, AutoApply)
    assert outcome.decision.rule_id == "r1"


async def test_decide_enrichment_review_proceeds_if_blessed_is_an_alt() -> None:
    # The review's top pick differs but it still considers the blessed category
    # plausible (an alternative) → proceed.
    outcome = await decide_enrichment(
        _snapshot(),
        _REQUEST.candidates,
        [_blessed_rule()],
        AutoActionCounters(),
        now=_NOW,
        model=_judge("groceries", alternatives=("dining",)),
    )
    assert isinstance(outcome, AutoApply)


async def test_decide_enrichment_review_escalates_on_disagreement() -> None:
    # The independent review does not find the blessed category plausible →
    # the one-way ratchet holds the auto-apply back to ASK.
    outcome = await decide_enrichment(
        _snapshot(),
        _REQUEST.candidates,
        [_blessed_rule()],
        AutoActionCounters(),
        now=_NOW,
        model=_judge("groceries"),
    )
    assert isinstance(outcome, AskHuman)
    assert isinstance(outcome.proposal.allocation, ProposedCategory)
    # Leads with the model's independent pick; offers the usual auto category.
    assert outcome.proposal.allocation.category == "groceries"
    assert CategoryId("dining") in outcome.proposal.alternatives


def _learned_rule() -> Rule:
    # Matches the snapshot's payee but is NOT blessed — it cannot gate an
    # auto-apply, only inform the ASK proposal.
    return Rule(
        id=RuleId("r2"),
        match=RuleMatch(payee_pattern="Blue Bottle"),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("dining"))
        ),
        trust=TrustState.CONFIRMED,
        source=RuleSource.LEARNED,
    )


def _capturing_model(prompts: list[str]) -> Model:
    """A FunctionModel that records the user prompt and answers 'dining'."""
    from pydantic_ai.messages import (
        ModelMessage,
        ModelResponse,
        ToolCallPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def call(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    prompts.append(str(part.content))
        assert info.output_tools
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args={
                        "category_id": "dining",
                        "confidence": "high",
                        "rationale": "x",
                    },
                )
            ]
        )

    return FunctionModel(call)


async def test_ask_prompt_carries_the_memo_and_the_rule_hint() -> None:
    # The model finally *sees* the memo and the payee's prior handling — the
    # context that turns "10683d916894?" guesses into informed proposals.
    prompts: list[str] = []
    snapshot = _snapshot().model_copy(update={"memo": "oat latte"})
    outcome = await decide_enrichment(
        snapshot,
        _REQUEST.candidates,
        [_learned_rule()],
        AutoActionCounters(),
        now=_NOW,
        model=_capturing_model(prompts),
    )
    assert isinstance(outcome, AskHuman)
    (prompt,) = prompts
    assert "Memo: oat latte" in prompt
    assert "Rule hint:" in prompt
    assert "Dining Out" in prompt


async def test_clean_context_review_never_sees_the_rule_hint() -> None:
    # §0.6: the independent judge must stay blind to the rule's choice — the
    # memo (a transaction fact) rides along, the hint never does.
    prompts: list[str] = []
    snapshot = _snapshot().model_copy(update={"memo": "oat latte"})
    outcome = await decide_enrichment(
        snapshot,
        _REQUEST.candidates,
        [_blessed_rule()],
        AutoActionCounters(),
        now=_NOW,
        model=_capturing_model(prompts),
    )
    assert isinstance(outcome, AutoApply)
    (prompt,) = prompts
    assert "Memo: oat latte" in prompt
    assert "Rule hint:" not in prompt


async def test_decide_enrichment_raises_on_a_hallucinated_id() -> None:
    # The raw-id-in-subject bug at its source: a hallucinated id never becomes
    # a proposal — the activity fails and retries instead.
    model = TestModel(
        custom_output_args={
            "category_id": "10683d916894",
            "confidence": "high",
            "rationale": "??",
        }
    )
    with pytest.raises(ValueError, match="unknown category id"):
        await decide_enrichment(
            _snapshot(),
            _REQUEST.candidates,
            [],
            AutoActionCounters(),
            now=_NOW,
            model=model,
        )


async def test_decide_enrichment_asks_when_no_trusted_rule() -> None:
    model = TestModel(
        custom_output_args={
            "category_id": "dining",
            "confidence": "medium",
            "rationale": "a coffee shop",
        }
    )
    outcome = await decide_enrichment(
        _snapshot(),
        _REQUEST.candidates,
        [],
        AutoActionCounters(),
        now=_NOW,
        model=model,
    )
    assert isinstance(outcome, AskHuman)
    assert isinstance(outcome.proposal.allocation, ProposedCategory)
    assert outcome.proposal.allocation.category == "dining"


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_OLLAMA"),
    reason="set YNAB_AGENT_LIVE_OLLAMA=1 to run the live Gemma smoke",
)
async def test_live_gemma_categorizes_a_coffee_shop() -> None:
    # SPEC §0.5 spike #2: real Gemma over Ollama returns usable content.
    out = await propose(_REQUEST)
    assert out.category_id in {"dining", "groceries"}
    assert out.rationale


def test_prompt_leads_with_the_stable_candidate_block() -> None:
    # Prefix-cache contract: the candidate list is byte-stable across calls
    # (one budget, one category list), so it must precede the per-call facts
    # — together with the static system prompt it forms the shared prefix
    # the model server can skip re-prefilling.
    from ynab_agent.agentic.enrich import _format_request

    prompt = _format_request(
        EnrichmentRequest(
            payee="Hulu",
            amount_display="-$13.07",
            memo="monthly",
            rule_hint="past transactions were filed under 'Streaming'",
            candidates=(CandidateCategory(id="c1", name="Streaming"),),
        )
    )
    assert prompt.index("Candidate categories:") < prompt.index("Payee:")
    assert prompt.rstrip().endswith(
        "Rule hint: past transactions were filed under 'Streaming'"
    )
