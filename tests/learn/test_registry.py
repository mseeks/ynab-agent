"""Tests for the durable rule registry's pure folds (SPEC §14, W5)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import ProposedCategory, ResolvedCategory
from ynab_agent.domain.effects import FeedRuleLearning, RuleLearningKind
from ynab_agent.domain.enums import DecidedBy, RuleSource, TrustState
from ynab_agent.domain.ids import CategoryId, RuleId
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.rule import RuleAction, RuleMatch
from ynab_agent.learn.events import ExplicitCommand
from ynab_agent.learn.registry import (
    AUDIT_CAP,
    RegistryState,
    bless_by_id,
    bless_rule,
    eligible_for_bless,
    mark_offered,
    pending_offers,
    record_learning,
    rules_for_payee,
)
from ynab_agent.learn.transitions import K_DEFAULT

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)
_SUBS = CategoryId("subscriptions")


def _decision(
    category: CategoryId = _SUBS, rule_id: RuleId | None = None
) -> Decision:
    return Decision(
        allocation=ResolvedCategory(category=category),
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=_NOW,
        rule_id=rule_id,
    )


def _confirm(
    payee: str = "Spotify", category: CategoryId = _SUBS
) -> FeedRuleLearning:
    return FeedRuleLearning(
        event=RuleLearningKind.CONFIRM,
        payee=payee,
        decision=_decision(category),
    )


def _record(
    state: RegistryState, feed: FeedRuleLearning, n: int = 0
) -> RegistryState:
    return record_learning(state, feed, now=_NOW, next_id=RuleId(f"r{n}"))


def test_confirm_creates_a_learned_rule() -> None:
    state = _record(RegistryState(), _confirm())
    assert len(state.rules) == 1
    assert state.rules[0].source is RuleSource.LEARNED
    assert state.rules[0].match.payee_pattern == "Spotify"
    assert len(state.audit) == 1


def test_nothing_learnable_leaves_state_unchanged() -> None:
    feed = FeedRuleLearning(event=RuleLearningKind.CONFIRM, payee="Spotify")
    state = _record(RegistryState(), feed)
    assert state == RegistryState()


def test_k_consistent_confirms_reach_trusted_and_are_eligible() -> None:
    state = RegistryState()
    for i in range(K_DEFAULT):
        state = _record(state, _confirm(), i)
    assert len(state.rules) == 1
    assert state.rules[0].trust is TrustState.TRUSTED
    # Trusted-by-consistency but learned: surfaced for the opt-in bless, not yet
    # auto-applicable (SPEC §14).
    assert eligible_for_bless(state) == state.rules


def test_oscillating_payee_never_becomes_eligible() -> None:
    state = RegistryState()
    for i in range(K_DEFAULT + 1):
        category = _SUBS if i % 2 == 0 else CategoryId("entertainment")
        state = _record(state, _confirm(category=category), i)
    # Two competing rules, neither with K consistent hits → nothing eligible.
    assert eligible_for_bless(state) == ()


def test_bless_makes_a_rule_trusted_and_no_longer_eligible() -> None:
    state = RegistryState()
    for i in range(K_DEFAULT):
        state = _record(state, _confirm(), i)
    command = ExplicitCommand(
        match=RuleMatch(payee_pattern="Spotify"),
        action=RuleAction(allocation=ProposedCategory(category=_SUBS)),
    )
    state = bless_rule(state, command, now=_NOW, next_id=RuleId("rb"))
    assert state.rules[0].source is RuleSource.HUMAN_EXPLICIT
    assert state.rules[0].trust is TrustState.TRUSTED
    assert eligible_for_bless(state) == ()


def test_correct_demotes_and_rewrites() -> None:
    state = _record(RegistryState(), _confirm(), 0)
    driving_rule_id = state.rules[0].id
    correction = FeedRuleLearning(
        event=RuleLearningKind.CORRECT,
        payee="Spotify",
        decision=_decision(CategoryId("software")),
        # The overturned decision names the rule that drove it, so the *right*
        # rule is rewritten and demoted (SPEC §3 rule 6) — not a fresh seed.
        prior=_decision(_SUBS, rule_id=driving_rule_id),
    )
    state = _record(state, correction, 1)
    assert len(state.rules) == 1
    assert state.rules[0].trust is TrustState.SUGGESTED
    assert isinstance(state.rules[0].action.allocation, ProposedCategory)
    assert state.rules[0].action.allocation.category == "software"


def _to_eligible(payee: str = "Spotify") -> RegistryState:
    """A registry holding one learned rule that has reached eligibility."""
    state = RegistryState()
    for i in range(K_DEFAULT):
        state = _record(state, _confirm(payee=payee), i)
    return state


def test_pending_offers_surfaces_eligible_unoffered_rules() -> None:
    state = _to_eligible()
    assert pending_offers(state) == eligible_for_bless(state)
    assert state.rules[0].offered_at is None


def test_mark_offered_drops_the_rule_from_pending_and_is_idempotent() -> None:
    state = _to_eligible()
    rule_id = state.rules[0].id
    marked = mark_offered(state, rule_id, now=_NOW)
    assert marked.rules[0].offered_at == _NOW
    assert pending_offers(marked) == ()
    # The rule is still eligible (just already offered) — never re-asked.
    assert eligible_for_bless(marked) == marked.rules
    # Marking again is a no-op (same state object returned).
    assert mark_offered(marked, rule_id, now=_NOW + datetime.timedelta(1)) is (
        marked
    )


def test_mark_offered_unknown_rule_is_a_noop() -> None:
    state = _to_eligible()
    assert mark_offered(state, RuleId("nope"), now=_NOW) is state


def test_bless_by_id_grants_autonomy_in_place() -> None:
    state = _to_eligible()
    rule_id = state.rules[0].id
    blessed = bless_by_id(state, rule_id, now=_NOW)
    assert blessed.rules[0].source is RuleSource.HUMAN_EXPLICIT
    assert blessed.rules[0].trust is TrustState.TRUSTED
    assert eligible_for_bless(blessed) == ()  # past eligibility now
    assert blessed.audit[-1].change.kind.value == "blessed"


def test_bless_by_id_is_a_noop_for_an_unknown_or_ineligible_rule() -> None:
    # Unknown id.
    state = _to_eligible()
    assert bless_by_id(state, RuleId("nope"), now=_NOW) is state
    # A not-yet-eligible (only confirmed, sub-K) rule cannot be blessed by id —
    # accepting a stale offer must not grant autonomy a payee hasn't earned.
    fresh = _record(RegistryState(), _confirm(payee="Hulu"), 0)
    assert fresh.rules[0].trust is TrustState.CONFIRMED
    assert bless_by_id(fresh, fresh.rules[0].id, now=_NOW) is fresh


def test_correction_clears_the_offered_marker_so_it_can_be_reoffered() -> None:
    state = _to_eligible()
    rule_id = state.rules[0].id
    state = mark_offered(state, rule_id, now=_NOW)
    assert state.rules[0].offered_at == _NOW
    correction = FeedRuleLearning(
        event=RuleLearningKind.CORRECT,
        payee="Spotify",
        decision=_decision(CategoryId("software")),
        prior=_decision(_SUBS, rule_id=rule_id),
    )
    state = _record(state, correction, 99)
    assert state.rules[0].offered_at is None  # re-offerable after re-earning


def test_blessed_rule_corrected_demotes_all_the_way_to_observe() -> None:
    state = _to_eligible()
    rule_id = state.rules[0].id
    state = bless_by_id(state, rule_id, now=_NOW)
    assert state.rules[0].source is RuleSource.HUMAN_EXPLICIT
    correction = FeedRuleLearning(
        event=RuleLearningKind.CORRECT,
        payee="Spotify",
        decision=_decision(CategoryId("software")),
        prior=_decision(_SUBS, rule_id=rule_id),
    )
    state = _record(state, correction, 100)
    # A correction of an auto-action drops the payee back to Observe (§14.2).
    assert state.rules[0].source is RuleSource.LEARNED
    assert state.rules[0].trust is TrustState.SUGGESTED
    assert eligible_for_bless(state) == ()


def test_rules_for_payee_matches_on_substring() -> None:
    state = _record(RegistryState(), _confirm(payee="Spotify"))
    assert rules_for_payee(state, "Spotify Premium") == state.rules
    assert rules_for_payee(state, "Netflix") == ()


def test_audit_tail_is_bounded() -> None:
    state = RegistryState()
    for i in range(AUDIT_CAP + 20):
        # Distinct payees so each event creates a rule and one audit entry.
        state = _record(state, _confirm(payee=f"Merchant {i}"), i)
    assert len(state.audit) == AUDIT_CAP
