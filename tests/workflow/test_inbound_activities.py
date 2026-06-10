"""Tests for the W2 inbound activities (SPEC §3, §5).

``interpret_inbound`` and ``converge`` are thin glue over the interpret/converge
agents (``tests/agentic``) and the verify policy (``tests/policy``). Their pure
helpers (the single proposed category, an end-state summary) are covered here.
The ``converge`` *orchestration* is also covered directly: it reads the current
YNAB state and decides before writing (SPEC §3 r3-4), so a no-op and a
divergence never issue a clobbering write.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from ynab_agent.budget.overspend import CategorySpend
from ynab_agent.domain.allocations import (
    PercentShare,
    ProposedCategory,
    ProposedSplit,
    ResolvedCategory,
    SplitLine,
)
from ynab_agent.domain.enums import Confidence, DecidedBy
from ynab_agent.domain.events import (
    Diverged,
    NeedsHuman,
    NoChange,
    Reapplied,
)
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    MessageId,
    ReceiptId,
    ThreadId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision, Proposal
from ynab_agent.domain.receipt import Receipt, ReceiptLineItem
from ynab_agent.domain.signals import ReceiptSignal, ReplySignal
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import TargetState, target_of
from ynab_agent.workflow.activities import (
    _proposed_category_id,
    _target_summary,
    converge,
    interpret_inbound,
)

if TYPE_CHECKING:
    import pytest

_NAMES = {"dining": "Dining Out", "coffee": "Coffee"}


def _proposal(allocation: object) -> Proposal:
    return Proposal(
        allocation=allocation,  # type: ignore[arg-type]
        confidence=Confidence.MEDIUM,
        rationale="because",
    )


def test_proposed_category_id_for_single_category() -> None:
    proposal = _proposal(ProposedCategory(category=CategoryId("dining")))
    assert _proposed_category_id(proposal) == "dining"


def test_proposed_category_id_none_for_split() -> None:
    split = ProposedSplit(
        lines=(
            SplitLine(
                share=PercentShare(percent=50), category=CategoryId("dining")
            ),
            SplitLine(
                share=PercentShare(percent=50), category=CategoryId("coffee")
            ),
        )
    )
    assert _proposed_category_id(_proposal(split)) is None


def test_proposed_category_id_none_for_no_proposal() -> None:
    assert _proposed_category_id(None) is None


def test_target_summary_names_category_and_memo() -> None:
    target = TargetState(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        memo="team lunch",
    )
    assert _target_summary(target, _NAMES) == "Dining Out — team lunch"


def test_target_summary_without_memo() -> None:
    target = TargetState(
        allocation=ResolvedCategory(category=CategoryId("coffee"))
    )
    assert _target_summary(target, _NAMES) == "Coffee"


def test_target_summary_handles_unreadable_state() -> None:
    assert _target_summary(None, _NAMES) == "(could not read)"


def test_target_summary_falls_back_to_id() -> None:
    target = TargetState(
        allocation=ResolvedCategory(category=CategoryId("mystery"))
    )
    assert _target_summary(target, _NAMES) == "mystery"


# ── converge orchestration: read current state before writing (SPEC §3 r3-4) ──
class _FakeYnab:
    """A YNAB client stub that records commits and serves a current state.

    ``read_back`` returns the current end-state; a ``commit`` lands the write so
    a subsequent ``read_back`` (the post-write verify) returns the target.
    """

    def __init__(self, current: TargetState | None) -> None:
        self._state = current
        self.commits: list[Decision] = []

    def category_spends(self) -> tuple[CategorySpend, ...]:
        zero = Money.from_milliunits(0)
        return tuple(
            CategorySpend(
                category=CategoryId(name),
                name=name.title(),
                budgeted=zero,
                activity=zero,
                balance=zero,
            )
            for name in ("dining", "gifts", "groceries")
        )

    def read_back(self, ynab_id: str) -> TargetState | None:
        return self._state

    def commit(self, ynab_id: str, decision: Decision) -> None:
        self.commits.append(decision)
        self._state = target_of(decision)


def _converge_snapshot() -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId("t-1"),
        account=AccountId("a1"),
        payee="Blue Bottle",
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, 30),
        category_id=CategoryId("dining"),
    )


def _reply(text: str) -> ReplySignal:
    return ReplySignal(
        thread_id=ThreadId("thread-1"),
        message_id=MessageId("m-1"),
        from_address="matthew@example.com",
        text=text,
    )


def _decision(category: str) -> Decision:
    return Decision(
        allocation=ResolvedCategory(category=CategoryId(category)),
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=datetime.datetime(2026, 5, 28, tzinfo=datetime.UTC),
    )


async def _run_converge(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: TargetState | None,
    prior: Decision | None,
    retarget_to: str = "gifts",
) -> tuple[object, _FakeYnab]:
    """Drive the converge activity with a stub client and a stubbed agent."""
    from ynab_agent.agentic import converge as converge_mod
    from ynab_agent.ynab.client import YnabClient

    fake = _FakeYnab(current)

    async def _fake_interpret(
        request: object, *, model: object = None
    ) -> converge_mod.RevisionTarget:
        return converge_mod.RevisionTarget(
            decision=converge_mod.RevisionDecision.RETARGET,
            category_id=retarget_to,
        )

    monkeypatch.setattr(converge_mod, "interpret_revision", _fake_interpret)
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    outcome = await converge(
        _converge_snapshot(), _reply("make it gifts"), prior
    )
    return outcome, fake


async def test_converge_no_op_skips_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The target already equals what the agent last applied: no write at all.
    outcome, fake = await _run_converge(
        monkeypatch,
        current=target_of(_decision("gifts")),
        prior=_decision("gifts"),
    )
    assert isinstance(outcome, NoChange)
    assert fake.commits == []


async def test_converge_adopts_a_landed_write_without_rewriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The target is already in YNAB but differs from the prior (a retried
    # converge whose write landed): re-applied, not NoChange, and not rewritten.
    outcome, fake = await _run_converge(
        monkeypatch,
        current=target_of(_decision("gifts")),
        prior=_decision("dining"),
    )
    assert isinstance(outcome, Reapplied)
    assert fake.commits == []


async def test_converge_surfaces_divergence_without_clobbering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A spouse recategorised the txn out of band: surface it, never overwrite.
    outcome, fake = await _run_converge(
        monkeypatch,
        current=target_of(_decision("groceries")),
        prior=_decision("dining"),
    )
    assert isinstance(outcome, Diverged)
    assert outcome.ynab_summary == "Groceries"
    assert outcome.requested_summary == "Gifts"
    assert fake.commits == []


async def test_converge_writes_then_verifies_a_real_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # YNAB still shows what the agent last applied and the target differs:
    # converge (one write), then verify the read-back.
    outcome, fake = await _run_converge(
        monkeypatch,
        current=target_of(_decision("dining")),
        prior=_decision("dining"),
    )
    assert isinstance(outcome, Reapplied)
    assert outcome.decision.allocation == ResolvedCategory(
        category=CategoryId("gifts")
    )
    assert len(fake.commits) == 1


# ── the receipt paths: deterministic, no model (SPEC §6) ─────────────────────
def _receipt() -> Receipt:
    return Receipt(
        id=ReceiptId("r1"),
        parked_at=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        merchant="Whole Foods",
        total=Money.from_currency("4.50"),
        line_items=(
            ReceiptLineItem(description="Corn Starch"),
            ReceiptLineItem(description="Paper Towels"),
        ),
    )


def _receipt_signal() -> ReceiptSignal:
    return ReceiptSignal(receipt_id=ReceiptId("r1"), receipt=_receipt())


def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A receipt revision must never reach the model — make it explosive."""
    from ynab_agent.agentic import converge as converge_mod

    async def _boom(request: object, *, model: object = None) -> object:
        raise AssertionError("the receipt path must not call the model")

    monkeypatch.setattr(converge_mod, "interpret_revision", _boom)


async def test_interpret_inbound_surfaces_receipt_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A matched receipt on an awaiting transaction is detail, not a reply:
    # deterministically ask with the items in front of the owner.
    outcome = await interpret_inbound(
        _receipt_signal(), _converge_snapshot(), None
    )
    question = outcome.question  # type: ignore[union-attr]
    assert "Whole Foods — $4.50" in question
    assert "Corn Starch, Paper Towels" in question


async def test_converge_receipt_folds_items_into_an_empty_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_model(monkeypatch)
    from ynab_agent.ynab.client import YnabClient

    prior = _decision("dining")
    fake = _FakeYnab(target_of(prior))
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    outcome = await converge(_converge_snapshot(), _receipt_signal(), prior)
    assert isinstance(outcome, Reapplied)
    assert len(fake.commits) == 1
    written = fake.commits[0]
    assert written.memo == "Corn Starch, Paper Towels"
    assert written.allocation == ResolvedCategory(
        category=CategoryId("dining")
    )  # the category never moves on a receipt
    assert written.decided_by is DecidedBy.AGENT


async def test_converge_receipt_appends_to_an_existing_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_model(monkeypatch)
    from ynab_agent.ynab.client import YnabClient

    snapshot = _converge_snapshot().model_copy(
        update={"memo": "weekly groceries"}
    )
    prior = _decision("dining").model_copy(update={"memo": "weekly groceries"})
    fake = _FakeYnab(target_of(prior))
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    outcome = await converge(snapshot, _receipt_signal(), prior)
    assert isinstance(outcome, Reapplied)
    assert fake.commits[0].memo == (
        "weekly groceries · Corn Starch, Paper Towels"
    )


async def test_converge_receipt_already_folded_is_no_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_model(monkeypatch)
    from ynab_agent.ynab.client import YnabClient

    live = TargetState(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        memo="Corn Starch, Paper Towels",
        approved=True,
    )
    fake = _FakeYnab(live)
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    outcome = await converge(
        _converge_snapshot(), _receipt_signal(), _decision("dining")
    )
    assert isinstance(outcome, NoChange)
    assert fake.commits == []


async def test_converge_receipt_folds_into_the_live_state_not_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE critical guard: the snapshot is frozen at materialization. By the
    # time a receipt arrives the owner may have re-chosen the category and
    # written a memo — the fold must build on the LIVE state, never revert.
    _no_model(monkeypatch)
    from ynab_agent.ynab.client import YnabClient

    live = TargetState(
        allocation=ResolvedCategory(category=CategoryId("gifts")),
        memo="for the kids",
        approved=True,
    )
    fake = _FakeYnab(live)
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    # The stale snapshot still says "dining" with no memo.
    outcome = await converge(
        _converge_snapshot(), _receipt_signal(), _decision("dining")
    )
    assert isinstance(outcome, Reapplied)
    written = fake.commits[0]
    assert written.allocation == ResolvedCategory(
        category=CategoryId("gifts")
    )  # the live category, never the stale one
    assert written.memo == "for the kids · Corn Starch, Paper Towels"


async def test_converge_receipt_never_approves_an_unapproved_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A receipt is detail: it must not approve a charge the owner declined
    # to decide (a LAPSED reopen), bypassing the gate and the floor.
    _no_model(monkeypatch)
    from ynab_agent.ynab.client import YnabClient

    live = TargetState(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        memo=None,
        approved=False,
    )
    fake = _FakeYnab(live)
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    outcome = await converge(_converge_snapshot(), _receipt_signal(), None)
    assert isinstance(outcome, Reapplied)
    assert fake.commits[0].approved is False  # preserved, never granted


async def test_converge_receipt_on_uncategorized_asks_a_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_model(monkeypatch)
    from ynab_agent.ynab.client import YnabClient

    # The LIVE read shows no single category (uncategorized or a split).
    fake = _FakeYnab(None)
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    outcome = await converge(_converge_snapshot(), _receipt_signal(), None)
    assert isinstance(outcome, NeedsHuman)
    assert "Whole Foods" in outcome.reason
    assert fake.commits == []
