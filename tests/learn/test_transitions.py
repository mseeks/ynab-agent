"""Tests for the W5 rule-learning transitions (SPEC §4.2, §9)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.enums import RuleSource, TrustState
from ynab_agent.domain.ids import CategoryId, RuleId
from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
from ynab_agent.learn.events import (
    ConfirmCategory,
    CorrectDecision,
    ExplicitCommand,
)
from ynab_agent.learn.transitions import (
    K_DEFAULT,
    RuleChangeKind,
    apply_learning,
    trust_for_hits,
)

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)
_MATCH = RuleMatch(payee_pattern="Blue Bottle")
_DINING = RuleAction(allocation=ProposedCategory(category=CategoryId("dining")))
_COFFEE = RuleAction(allocation=ProposedCategory(category=CategoryId("coffee")))


def _confirm(
    action: RuleAction = _DINING, rule_id: RuleId | None = None
) -> ConfirmCategory:
    return ConfirmCategory(match=_MATCH, action=action, rule_id=rule_id)


def _only(rules: tuple[Rule, ...]) -> Rule:
    assert len(rules) == 1
    return rules[0]


# ── trust_for_hits (the K-counter mapping) ──────────────────────────────────
def test_trust_for_hits_ladder() -> None:
    assert trust_for_hits(0, K_DEFAULT) is TrustState.SUGGESTED
    assert trust_for_hits(1, K_DEFAULT) is TrustState.CONFIRMED
    assert trust_for_hits(2, K_DEFAULT) is TrustState.CONFIRMED
    assert trust_for_hits(3, K_DEFAULT) is TrustState.TRUSTED
    assert trust_for_hits(9, K_DEFAULT) is TrustState.TRUSTED


# ── Confirm ──────────────────────────────────────────────────────────────────
def test_first_confirm_creates_a_confirmed_rule() -> None:
    out = apply_learning((), _confirm(), now=_NOW, next_id=RuleId("r1"))
    rule = _only(out.rules)
    assert out.change.kind is RuleChangeKind.CREATED
    assert rule.trust is TrustState.CONFIRMED
    assert rule.hits == 1
    assert rule.source is RuleSource.LEARNED
    assert rule.last_confirmed_at == _NOW


def test_k_consistent_confirms_reach_trusted() -> None:
    rules: tuple[Rule, ...] = ()
    for _ in range(K_DEFAULT):
        out = apply_learning(rules, _confirm(), now=_NOW, next_id=RuleId("r1"))
        rules = out.rules
    rule = _only(rules)
    assert rule.hits == K_DEFAULT
    assert rule.trust is TrustState.TRUSTED


def test_confirm_by_rule_id_strengthens_that_rule() -> None:
    rule = Rule(
        id=RuleId("r1"),
        match=_MATCH,
        action=_DINING,
        trust=TrustState.CONFIRMED,
        hits=2,
        source=RuleSource.LEARNED,
    )
    out = apply_learning(
        (rule,),
        _confirm(rule_id=RuleId("r1")),
        now=_NOW,
        next_id=RuleId("unused"),
    )
    updated = _only(out.rules)
    assert out.change.kind is RuleChangeKind.STRENGTHENED
    assert updated.hits == 3
    assert updated.trust is TrustState.TRUSTED


def test_confirming_a_different_action_builds_a_separate_rule() -> None:
    dining_rule = Rule(
        id=RuleId("r1"),
        match=_MATCH,
        action=_DINING,
        trust=TrustState.CONFIRMED,
        hits=2,
        source=RuleSource.LEARNED,
    )
    out = apply_learning(
        (dining_rule,),
        _confirm(action=_COFFEE),
        now=_NOW,
        next_id=RuleId("r2"),
    )
    assert out.change.kind is RuleChangeKind.CREATED
    assert {r.id for r in out.rules} == {"r1", "r2"}
    # The unrelated dining rule is untouched — per-rule K (SPEC §4.2).
    dining = next(r for r in out.rules if r.id == "r1")
    assert dining.hits == 2


# ── Correct ──────────────────────────────────────────────────────────────────
def test_correction_rewrites_and_demotes_the_driving_rule() -> None:
    trusted = Rule(
        id=RuleId("r1"),
        match=_MATCH,
        action=_DINING,
        trust=TrustState.TRUSTED,
        hits=5,
        source=RuleSource.LEARNED,
    )
    out = apply_learning(
        (trusted,),
        CorrectDecision(
            match=_MATCH, action=_COFFEE, prior_rule_id=RuleId("r1")
        ),
        now=_NOW,
        next_id=RuleId("unused"),
    )
    updated = _only(out.rules)
    assert out.change.kind is RuleChangeKind.REWRITTEN
    assert updated.action == _COFFEE
    assert updated.trust is TrustState.SUGGESTED
    assert updated.hits == 0
    assert updated.last_corrected_at == _NOW


def test_correction_without_a_prior_rule_seeds_a_suggested_rule() -> None:
    out = apply_learning(
        (),
        CorrectDecision(match=_MATCH, action=_COFFEE, prior_rule_id=None),
        now=_NOW,
        next_id=RuleId("r1"),
    )
    rule = _only(out.rules)
    assert out.change.kind is RuleChangeKind.CREATED
    assert rule.trust is TrustState.SUGGESTED
    assert rule.action == _COFFEE
    assert rule.last_corrected_at == _NOW


def test_oscillation_never_reaches_trusted() -> None:
    # Confirm A, then a correction to B, then confirm B: never K-consistent.
    rules: tuple[Rule, ...] = ()
    out = apply_learning(rules, _confirm(), now=_NOW, next_id=RuleId("r1"))
    out = apply_learning(
        out.rules,
        CorrectDecision(
            match=_MATCH, action=_COFFEE, prior_rule_id=RuleId("r1")
        ),
        now=_NOW,
        next_id=RuleId("x"),
    )
    out = apply_learning(
        out.rules, _confirm(action=_COFFEE), now=_NOW, next_id=RuleId("x")
    )
    rule = _only(out.rules)
    assert rule.trust is not TrustState.TRUSTED


# ── Explicit command ─────────────────────────────────────────────────────────
def test_explicit_command_blesses_straight_to_trusted() -> None:
    out = apply_learning(
        (),
        ExplicitCommand(match=_MATCH, action=_DINING),
        now=_NOW,
        next_id=RuleId("r1"),
    )
    rule = _only(out.rules)
    assert out.change.kind is RuleChangeKind.BLESSED
    assert rule.trust is TrustState.TRUSTED
    assert rule.source is RuleSource.HUMAN_EXPLICIT


def test_explicit_command_upgrades_an_existing_learned_rule() -> None:
    learned = Rule(
        id=RuleId("r1"),
        match=_MATCH,
        action=_DINING,
        trust=TrustState.SUGGESTED,
        hits=1,
        source=RuleSource.LEARNED,
    )
    out = apply_learning(
        (learned,),
        ExplicitCommand(match=_MATCH, action=_DINING),
        now=_NOW,
        next_id=RuleId("unused"),
    )
    updated = _only(out.rules)
    assert updated.trust is TrustState.TRUSTED
    assert updated.source is RuleSource.HUMAN_EXPLICIT


def test_confirming_a_human_blessed_rule_keeps_it_trusted() -> None:
    blessed = Rule(
        id=RuleId("r1"),
        match=_MATCH,
        action=_DINING,
        trust=TrustState.TRUSTED,
        hits=0,
        source=RuleSource.HUMAN_EXPLICIT,
    )
    out = apply_learning(
        (blessed,),
        _confirm(rule_id=RuleId("r1")),
        now=_NOW,
        next_id=RuleId("unused"),
    )
    updated = _only(out.rules)
    assert updated.trust is TrustState.TRUSTED
