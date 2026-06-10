"""The enrichment agent: propose a category for a transaction (SPEC §4.1).

The agentic half of the ``enrich`` step. Given a transaction's facts and the
candidate budget categories, a Pydantic AI agent picks the best category, rates
its confidence, and explains why. The deterministic gate (``policy.gate``) then
decides auto-apply vs. ask — confidence here is *framing only*, never a gate
(principle 6). :func:`to_proposal` maps the agent's structured output onto the
domain :class:`~ynab_agent.domain.proposal.Proposal`.

The model is injected per run so tests drive a ``TestModel``/``FunctionModel``
offline; production uses :func:`~ynab_agent.agentic.model.build_model` (Ollama).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.model import run_structured
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import Confidence, ReviewVerdict, SourceKind
from ynab_agent.domain.events import AskHuman, AutoApply
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.proposal import Proposal, ProposalSource
from ynab_agent.policy.gate import (
    GateVerdict,
    build_auto_decision,
    evaluate_gate,
    matching_rules,
)

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable

    from pydantic_ai.models import Model

    from ynab_agent.domain.events import EnrichmentOutcome
    from ynab_agent.domain.rule import Rule
    from ynab_agent.domain.transaction import YnabSnapshot
    from ynab_agent.policy.floor import AutoActionCounters


class CandidateCategory(Frozen):
    """One budget category the agent may choose from."""

    id: str
    name: str


class EnrichmentRequest(Frozen):
    """The facts the agent reasons over to propose a category."""

    payee: str
    amount_display: str
    candidates: tuple[CandidateCategory, ...] = Field(min_length=1)
    memo: str | None = None
    rule_hint: str | None = None


class EnrichmentSuggestion(Frozen):
    """The agent's structured proposal (mapped to a domain Proposal)."""

    category_id: str
    confidence: Confidence
    rationale: str
    alternatives: tuple[str, ...] = ()


_SYSTEM_PROMPT = """\
You categorize a single bank transaction for a personal budget. You are given
a list of candidate budget categories (each with an id and a name), then the
transaction's facts: payee, amount, an optional memo, and an optional hint
from a learned rule.

Choose the SINGLE best category for the transaction. Your `category_id` MUST be
one of the provided candidate ids — never invent one. Also list up to 2
`alternatives`: the next-most-likely candidate ids (also from the list, never
the chosen one) the owner might prefer — or leave empty if none fit. Rate your
confidence (high / medium / low) and give a one-sentence rationale. Confidence
is framing only; a human or a trusted rule decides whether to auto-apply."""

_AGENT: Agent[None, EnrichmentSuggestion] = Agent(
    output_type=EnrichmentSuggestion,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: EnrichmentRequest) -> str:
    """Render the request as the agent's user prompt.

    Ordered for KV prefix-cache reuse: the candidate category list is
    byte-stable across calls (one budget, one category list), so it leads
    and extends the shared prefix the server can skip re-prefilling; the
    per-call facts (payee, amount, memo, hint) trail. With dozens of
    categories the stable block is most of the prompt.
    """
    lines = ["Candidate categories:"]
    lines.extend(f"  - {c.name} (id: {c.id})" for c in request.candidates)
    lines.append(f"Payee: {request.payee}")
    lines.append(f"Amount: {request.amount_display}")
    if request.memo:
        lines.append(f"Memo: {request.memo}")
    if request.rule_hint:
        lines.append(f"Rule hint: {request.rule_hint}")
    return "\n".join(lines)


async def propose(
    request: EnrichmentRequest, *, model: Model | None = None
) -> EnrichmentSuggestion:
    """Run the enrichment agent for one transaction (SPEC §4.1).

    Args:
        request: The transaction facts and candidate categories.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The agent's structured category suggestion.
    """
    return await run_structured(
        _AGENT,
        _format_request(request),
        output_type=EnrichmentSuggestion,
        model=model,
    )


def validate_suggestion(
    suggestion: EnrichmentSuggestion,
    candidates: tuple[CandidateCategory, ...],
) -> EnrichmentSuggestion:
    """Reject a hallucinated category id; drop hallucinated alternatives.

    The model's ``category_id`` MUST be a candidate id — an invented one would
    flow into the proposal and surface as a raw id in the email subject (and a
    write against it would 400). Raising lets the enclosing activity retry,
    which re-runs the model. Alternatives are framing only, so invalid ones are
    silently dropped rather than failing the run.
    """
    valid = {c.id for c in candidates}
    if suggestion.category_id not in valid:
        msg = (
            f"model proposed unknown category id "
            f"{suggestion.category_id!r} (not a candidate)"
        )
        raise ValueError(msg)
    kept = tuple(a for a in suggestion.alternatives if a in valid)
    if kept == suggestion.alternatives:
        return suggestion
    return suggestion.model_copy(update={"alternatives": kept})


def to_proposal(suggestion: EnrichmentSuggestion) -> Proposal:
    """Map the agent's suggestion onto a domain Proposal (SPEC §4.1).

    The model's runner-up ids become the proposal's alternatives (the chosen
    one filtered out defensively), surfaced in the email so the owner can pick
    one at a glance.
    """
    alternatives = tuple(
        CategoryId(alt)
        for alt in suggestion.alternatives
        if alt and alt != suggestion.category_id
    )
    return Proposal(
        allocation=ProposedCategory(
            category=CategoryId(suggestion.category_id)
        ),
        confidence=suggestion.confidence,
        rationale=suggestion.rationale,
        sources=(ProposalSource(kind=SourceKind.MODEL),),
        alternatives=alternatives,
    )


def _blessed_category_id(rule: Rule) -> str | None:
    """The single category a rule auto-applies, or ``None`` for a split.

    The safety review compares a single independent category against the rule's;
    a split action has no single category to judge, so the review is skipped for
    it (learned-eligible rules are always single-category, so this is rare).
    """
    allocation = rule.action.allocation
    if isinstance(allocation, ProposedCategory):
        return str(allocation.category)
    return None


def _rule_hint(
    snapshot: YnabSnapshot,
    rules: tuple[Rule, ...],
    candidates: tuple[CandidateCategory, ...],
) -> str | None:
    """A one-line hint from the first matching rule, for the ASK proposal.

    A matching-but-not-blessed rule (learned, or eligible-awaiting-blessing)
    still encodes how this payee was handled before — exactly the context the
    proposing model should weigh. Only the ASK-path prompt gets it; the §0.6
    clean-context review must stay blind to the rule's choice.
    """
    names = {c.id: c.name for c in candidates}
    for rule in matching_rules(rules, snapshot):
        category_id = _blessed_category_id(rule)
        if category_id is not None and category_id in names:
            return (
                f"past transactions from this payee were filed under "
                f"'{names[category_id]}'"
            )
    return None


def review_auto_apply(
    blessed_category_id: str, suggestion: EnrichmentSuggestion
) -> ReviewVerdict:
    """The agent-powered safety review's one-way ratchet (SPEC §0.6 Layer 2).

    An independent, *clean-context* model categorization (run with no knowledge
    of the rule's choice, to stay unbiased) judges the impending auto-apply: it
    ``PROCEED``s only if it considers the blessed category plausible — the
    category it chose or listed as an alternative — and otherwise
    ``ESCALATE_TO_HUMAN``. It can only hold an auto-apply back, never grant one,
    so the deterministic gate still authorizes (principle 6).
    """
    plausible = {suggestion.category_id, *suggestion.alternatives}
    if blessed_category_id in plausible:
        return ReviewVerdict.PROCEED
    return ReviewVerdict.ESCALATE_TO_HUMAN


def _escalation_proposal(
    suggestion: EnrichmentSuggestion, blessed_category_id: str
) -> Proposal:
    """The proposal emailed when the review holds an auto-apply back.

    Leads with the model's independent pick and offers the usual auto category
    as an alternative, so the owner sees both views and the disagreement that
    triggered the question.
    """
    base = to_proposal(suggestion)
    blessed = CategoryId(blessed_category_id)
    # to_proposal always builds a single-category allocation; narrow for mypy.
    allocation = base.allocation
    top = (
        allocation.category
        if isinstance(allocation, ProposedCategory)
        else None
    )
    alternatives = base.alternatives
    if blessed != top and blessed not in alternatives:
        alternatives = (blessed, *alternatives)
    return base.model_copy(
        update={
            "alternatives": alternatives,
            "rationale": (
                "I'd usually auto-file this payee, but this charge looked "
                "different from the usual — confirming before I apply it."
            ),
        }
    )


async def decide_enrichment(
    snapshot: YnabSnapshot,
    candidates: tuple[CandidateCategory, ...],
    rules: Iterable[Rule],
    counters: AutoActionCounters,
    *,
    now: datetime.datetime,
    model: Model | None = None,
) -> EnrichmentOutcome:
    """Compose the enrich step: gate, then a safety review (SPEC §4.1, §0.6).

    The deterministic gate decides autonomy from the rules alone — a single
    blessed rule may auto-apply *its* action (the model never authorizes a
    write, principle 6). Before an auto-apply lands, an independent
    *clean-context* model review judges it (:func:`review_auto_apply`); a
    disagreement holds it back to ASK (a one-way ratchet — it can only veto). A
    gated ASK runs the same model to produce the best-guess proposal the email
    shows.

    Args:
        snapshot: The transaction being enriched.
        candidates: The budget categories the agent may choose from.
        rules: The rules in scope for the gate.
        counters: The auto-action counters the hard floor reads.
        now: The decision timestamp (the activity supplies it).
        model: A model override for tests; defaults to Ollama/Gemma.

    Returns:
        ``AutoApply`` when a blessed rule gates it *and* the review proceeds,
        else ``AskHuman``.
    """
    rules = tuple(rules)
    gate = evaluate_gate(snapshot, rules, counters)
    if gate.verdict is GateVerdict.AUTO and gate.rule_id is not None:
        rule = next((r for r in rules if r.id == gate.rule_id), None)
        if rule is not None:
            decision = build_auto_decision(rule, snapshot, now)
            blessed_category_id = _blessed_category_id(rule)
            if blessed_category_id is None:
                return AutoApply(decision=decision)
            # Clean context: the independent judge never sees the rule's
            # choice — the memo is a transaction fact, so it rides along.
            suggestion = validate_suggestion(
                await propose(
                    EnrichmentRequest(
                        payee=snapshot.payee,
                        amount_display=str(snapshot.amount),
                        candidates=candidates,
                        memo=snapshot.memo,
                    ),
                    model=model,
                ),
                candidates,
            )
            if (
                review_auto_apply(blessed_category_id, suggestion)
                is ReviewVerdict.PROCEED
            ):
                return AutoApply(decision=decision)
            return AskHuman(
                proposal=_escalation_proposal(suggestion, blessed_category_id)
            )

    # The ASK-path proposal gets every fact available: the memo and any
    # matching rule's history (the clean-context review above never does).
    suggestion = validate_suggestion(
        await propose(
            EnrichmentRequest(
                payee=snapshot.payee,
                amount_display=str(snapshot.amount),
                candidates=candidates,
                memo=snapshot.memo,
                rule_hint=_rule_hint(snapshot, rules, candidates),
            ),
            model=model,
        ),
        candidates,
    )
    return AskHuman(proposal=to_proposal(suggestion))
