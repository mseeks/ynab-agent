"""Tests for the converge-to-target reconciliation (SPEC §3 rules 2-4)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import (
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.enums import ClearedState, DecidedBy
from ynab_agent.domain.events import VerifyOutcome
from ynab_agent.domain.ids import AccountId, CategoryId, YnabTransactionId
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import (
    PrecommitAction,
    TargetState,
    classify_verify,
    content_hash,
    needs_write,
    precommit_action,
    reconciliation_blocks,
    target_of,
)

_EPOCH = datetime.datetime(2026, 5, 28, tzinfo=datetime.UTC)


def _target(category: str = "dining", memo: str | None = None) -> TargetState:
    return TargetState(
        allocation=ResolvedCategory(category=CategoryId(category)), memo=memo
    )


def _split_target(*cats: str) -> TargetState:
    return TargetState(
        allocation=ResolvedSplit(
            lines=tuple(
                ResolvedSplitLine(
                    category=CategoryId(c), amount=Money.from_milliunits(-1000)
                )
                for c in cats
            )
        )
    )


def test_content_hash_is_stable_and_distinguishing() -> None:
    assert content_hash(_target()) == content_hash(_target())
    assert content_hash(_target("dining")) != content_hash(_target("gifts"))
    assert content_hash(_target(memo="a")) != content_hash(_target(memo="b"))


def test_content_hash_is_order_insensitive_for_splits() -> None:
    # YNAB may return a split's lines in any order; two equal splits must hash
    # the same so a read-back verifies as MATCH (SPEC §3 r4).
    assert content_hash(_split_target("gifts", "groceries")) == content_hash(
        _split_target("groceries", "gifts")
    )
    # A genuinely different split still hashes differently.
    assert content_hash(_split_target("gifts", "groceries")) != content_hash(
        _split_target("gifts", "dining")
    )


def test_needs_write_skips_an_equal_state() -> None:
    assert not needs_write(_target(), _target())
    assert needs_write(_target("dining"), _target("gifts"))
    assert needs_write(None, _target())


def test_classify_verify_outcomes() -> None:
    target = _target("dining")
    assert classify_verify(target, target) is VerifyOutcome.MATCH
    assert classify_verify(None, target) is VerifyOutcome.COULD_NOT_CONFIRM
    assert classify_verify(_target("gifts"), target) is VerifyOutcome.DIVERGED


def test_target_of_projects_a_decision() -> None:
    decision = Decision(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        memo="coffee",
        approved=True,
        decided_by=DecidedBy.AGENT,
        decided_at=_EPOCH,
    )
    target = target_of(decision)
    assert target.memo == "coffee"
    assert content_hash(target) == content_hash(_target(memo="coffee"))


def test_reconciliation_guard() -> None:
    base: dict[str, object] = {
        "ynab_id": YnabTransactionId("t1"),
        "account": AccountId("a1"),
        "payee": "Blue Bottle",
        "amount": Money.from_currency("-4.50"),
        "txn_date": datetime.date(2026, 5, 28),
    }
    reconciled = YnabSnapshot.model_validate(
        {**base, "cleared": ClearedState.RECONCILED}
    )
    closed_month = YnabSnapshot.model_validate({**base, "month_closed": True})
    cleared = YnabSnapshot.model_validate(
        {**base, "cleared": ClearedState.CLEARED}
    )
    assert reconciliation_blocks(reconciled)
    assert reconciliation_blocks(closed_month)
    assert not reconciliation_blocks(cleared)


# ── precommit_action: the pre-write converge decision (SPEC §3 r3-4) ─────────
def test_precommit_no_change_when_target_equals_prior() -> None:
    # YNAB already holds the prior state and the target asks nothing new.
    action = precommit_action(
        _target("dining"), _target("dining"), _target("dining")
    )
    assert action is PrecommitAction.NO_CHANGE


def test_precommit_already_target_when_write_already_landed() -> None:
    # current == target but differs from the prior: a retried converge whose
    # write landed (or an out-of-band edit that happens to match the target).
    # Adopt it as re-applied rather than rewriting — and never as NO_CHANGE,
    # which would revert the workflow's decision to the stale prior.
    assert (
        precommit_action(_target("gifts"), _target("gifts"), _target("dining"))
        is PrecommitAction.ALREADY_TARGET
    )
    # No prior (a revision entered from LAPSED) with the target already present.
    assert (
        precommit_action(_target("gifts"), _target("gifts"), None)
        is PrecommitAction.ALREADY_TARGET
    )


def test_precommit_diverged_on_out_of_band_recategorisation() -> None:
    # A write is needed, but YNAB drifted to a different non-empty category than
    # the agent last applied (a spouse edited it directly) — surface it BEFORE
    # writing, never clobber.
    action = precommit_action(
        _target("groceries"), _target("gifts"), _target("dining")
    )
    assert action is PrecommitAction.DIVERGED


def test_precommit_writes_when_current_matches_prior() -> None:
    # The normal revision: YNAB still shows what the agent last applied, and the
    # target differs — converge.
    action = precommit_action(
        _target("dining"), _target("gifts"), _target("dining")
    )
    assert action is PrecommitAction.WRITE


def test_precommit_writes_from_lapsed_without_a_prior() -> None:
    # No prior baseline (entered from LAPSED): there is nothing of ours to
    # clobber, so a differing target simply writes.
    action = precommit_action(_target("dining"), _target("gifts"), None)
    assert action is PrecommitAction.WRITE


def test_precommit_writes_when_current_is_unreadable() -> None:
    # current None (a split or uncategorized) is not a divergence — there is no
    # non-empty state to protect — so it writes (and the read-back classifies).
    action = precommit_action(None, _target("gifts"), _target("dining"))
    assert action is PrecommitAction.WRITE


def test_precommit_memo_only_drift_is_not_divergence() -> None:
    # A spouse changing only the memo (same category) is not a divergence; the
    # allocation still matches the prior, so the converge proceeds.
    action = precommit_action(
        _target("dining", memo="their note"),
        _target("gifts"),
        _target("dining", memo="orig"),
    )
    assert action is PrecommitAction.WRITE
