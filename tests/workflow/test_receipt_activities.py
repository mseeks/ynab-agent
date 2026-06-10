"""Tests for the W4 join activities (SPEC §6).

The deterministic matching envelope (pool, exact shortcut, id validation)
and the ledger / signal plumbing, all offline via fakes.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.ids import AccountId, ReceiptId, YnabTransactionId
from ynab_agent.domain.money import Money
from ynab_agent.domain.receipt import Receipt
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.join.match import Ambiguous, ConfidentMatch, NoMatch
from ynab_agent.workflow import receipt_activities, temporal_client
from ynab_agent.workflow.receipt_activities import (
    candidate_pool,
    exact_single_match,
)

_NOW = datetime.datetime(2026, 6, 10, 12, 0, tzinfo=datetime.UTC)


def _txn(
    tid: str, amount: str, *, day: int = 8, payee: str = "Whole Foods"
) -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId(tid),
        account=AccountId("a1"),
        payee=payee,
        amount=Money.from_currency(amount),
        txn_date=datetime.date(2026, 6, day),
    )


def _receipt(
    *,
    total: str | None = "23.48",
    date: datetime.date | None = datetime.date(2026, 6, 8),
    status: ReceiptStatus = ReceiptStatus.PARKED,
) -> Receipt:
    return Receipt(
        id=ReceiptId("r1"),
        parked_at=_NOW,
        merchant="Whole Foods",
        total=Money.from_currency(total) if total else None,
        date=date,
        status=status,
        source_thread_id=None,
    )


# ── the deterministic envelope ───────────────────────────────────────────────
def test_pool_admits_by_amount_or_date_and_ranks_by_closeness() -> None:
    txns = (
        _txn("far", "-99.99", day=20),  # neither close → excluded
        _txn("date-close", "-50.00", day=9),  # date within window
        _txn("amount-close", "-23.48", day=20),  # exact amount, far date
    )
    pool = candidate_pool(_receipt(), txns)
    assert [str(t.ynab_id) for t in pool] == ["amount-close", "date-close"]


def test_pool_merchant_only_receipt_gets_the_window() -> None:
    receipt = _receipt(total=None, date=None)
    txns = (_txn("t1", "-5.00"), _txn("t2", "-9.00"))
    assert len(candidate_pool(receipt, txns)) == 2


def test_pool_is_capped() -> None:
    txns = tuple(_txn(f"t{i}", "-23.48") for i in range(30))
    assert len(candidate_pool(_receipt(), txns)) == 12


def test_exact_single_match_is_deterministic() -> None:
    pool = (_txn("hit", "-23.48"), _txn("near", "-23.50"))
    match = exact_single_match(_receipt(), pool)
    assert match is not None
    assert match.txn_id == "hit"


def test_two_exact_candidates_defer_to_the_model() -> None:
    # The two-coffees case (SPEC §6): never guess between equals.
    pool = (_txn("a", "-23.48", day=8), _txn("b", "-23.48", day=8))
    assert exact_single_match(_receipt(), pool) is None


def test_exact_match_outside_the_date_window_defers() -> None:
    pool = (_txn("late", "-23.48", day=12),)
    assert exact_single_match(_receipt(), pool) is None


class _FakeYnab:
    def __init__(self, txns: tuple[YnabSnapshot, ...]) -> None:
        self._txns = txns
        self.since: list[datetime.date] = []

    def recent(self, since: datetime.date) -> tuple[YnabSnapshot, ...]:
        self.since.append(since)
        return self._txns


def _stub_ynab(
    monkeypatch: pytest.MonkeyPatch, txns: tuple[YnabSnapshot, ...]
) -> _FakeYnab:
    from ynab_agent.ynab.client import YnabClient

    fake = _FakeYnab(txns)
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: fake))
    return fake


def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from ynab_agent.agentic import match as match_mod

    async def _boom(request: object, *, model: object = None) -> object:
        raise AssertionError("the deterministic path must not call the model")

    monkeypatch.setattr(match_mod, "match_receipt", _boom)


async def test_match_activity_empty_pool_is_no_match_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_model(monkeypatch)
    _stub_ynab(monkeypatch, (_txn("far", "-99.99", day=25),))
    outcome = await receipt_activities.match_receipt(_receipt())
    assert isinstance(outcome, NoMatch)


async def test_match_activity_exact_single_skips_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_model(monkeypatch)
    _stub_ynab(monkeypatch, (_txn("hit", "-23.48"),))
    outcome = await receipt_activities.match_receipt(_receipt())
    assert isinstance(outcome, ConfidentMatch)
    assert outcome.txn_id == "hit"


async def test_match_activity_validates_the_models_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fuzzy pool reaches the model; a hallucinated id in its verdict parks
    # the receipt (NoMatch) rather than attaching it anywhere.
    from ynab_agent.agentic import match as match_mod

    _stub_ynab(
        monkeypatch, (_txn("a", "-23.48", day=8), _txn("b", "-23.48", day=8))
    )

    async def _hallucinate(
        request: object, *, model: object = None
    ) -> match_mod.MatchVerdict:
        return match_mod.MatchVerdict(
            decision=match_mod.MatchDecision.MATCH, txn_id="invented"
        )

    monkeypatch.setattr(match_mod, "match_receipt", _hallucinate)
    outcome = await receipt_activities.match_receipt(_receipt())
    assert isinstance(outcome, NoMatch)


async def test_match_activity_passes_a_fuzzy_pool_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ynab_agent.agentic import match as match_mod

    _stub_ynab(
        monkeypatch, (_txn("a", "-23.48", day=8), _txn("b", "-23.48", day=8))
    )
    seen: list[match_mod.MatchRequest] = []

    async def _ambiguous(
        request: match_mod.MatchRequest, *, model: object = None
    ) -> match_mod.MatchVerdict:
        seen.append(request)
        return match_mod.MatchVerdict(
            decision=match_mod.MatchDecision.AMBIGUOUS,
            candidate_ids=("a", "b"),
        )

    monkeypatch.setattr(match_mod, "match_receipt", _ambiguous)
    outcome = await receipt_activities.match_receipt(_receipt())
    assert isinstance(outcome, Ambiguous)
    assert {str(c) for c in outcome.candidates} == {"a", "b"}
    assert {c.id for c in seen[0].candidates} == {"a", "b"}


# ── ledger + signal plumbing ─────────────────────────────────────────────────
class _FakeHandle:
    def __init__(self, raw: object, *, running: bool = True) -> None:
        self._raw = raw
        self._running = running
        self.signals: list[tuple[str, object]] = []

    async def query(self, name: str, *args: object, **kwargs: object) -> object:
        return self._raw

    async def describe(self) -> object:
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        if not self._running:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return type("D", (), {"status": WorkflowExecutionStatus.RUNNING})()

    async def signal(self, name: str, arg: object) -> None:
        self.signals.append((name, arg))


class _FakeTemporal:
    def __init__(self, raw: object = None, *, running: bool = True) -> None:
        self._raw = raw
        self.started: list[tuple[object, object, dict[str, object]]] = []
        self.handle = _FakeHandle(raw, running=running)

    def get_workflow_handle(self, workflow_id: str) -> _FakeHandle:
        return self.handle

    async def start_workflow(
        self, workflow: object, arg: object, **kwargs: object
    ) -> None:
        self.started.append((workflow, arg, kwargs))


def test_signal_match_signals_a_running_w2_with_the_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    fake = _FakeTemporal(raw=receipt.model_dump(mode="json"))
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(receipt_activities.signal_match("t-9", "r1"))
    assert fake.started == []  # never starts (re-triage) — signals only
    name, signal = fake.handle.signals[0]
    assert name == "submit_inbound"
    assert signal.receipt.merchant == "Whole Foods"  # type: ignore[attr-defined]


def test_signal_match_folds_directly_when_no_w2_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hand-approved / pre-install / archived charge has no live W2 and
    # must NOT get one (a fresh proposal email for a settled charge): the
    # memo is folded directly, memo-only, and verified by re-read.
    receipt = _receipt()
    fake = _FakeTemporal(raw=receipt.model_dump(mode="json"), running=False)
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    folded: list[tuple[str, object]] = []

    async def _fold(txn_id: str, r: object) -> None:
        folded.append((txn_id, r))

    monkeypatch.setattr(receipt_activities, "_fold_memo_directly", _fold)
    asyncio.run(receipt_activities.signal_match("t-9", "r1"))
    assert fake.started == []
    assert fake.handle.signals == []
    assert folded[0][0] == "t-9"


def test_signal_match_fails_loud_when_the_ledger_forgot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Failing BEFORE MATCHED is saved means a later re-check retries cleanly.
    fake = _FakeTemporal(raw=None)
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    with pytest.raises(RuntimeError, match="not in the ledger"):
        asyncio.run(receipt_activities.signal_match("t-9", "r1"))
    assert fake.started == []


def test_save_receipt_status_signals_with_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTemporal()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(
        receipt_activities.save_receipt_status("r1", ReceiptStatus.MATCHED)
    )
    workflow, _params, kwargs = fake.started[0]
    assert workflow == "ReceiptLedgerWorkflow"
    assert kwargs["start_signal"] == "set_status"
    request = kwargs["start_signal_args"][0]  # type: ignore[index]
    assert request.receipt_id == "r1"
    assert request.status is ReceiptStatus.MATCHED


def test_start_join_uses_a_per_receipt_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTemporal()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(receipt_activities.start_join(_receipt()))
    workflow, _params, kwargs = fake.started[0]
    assert workflow == "ReceiptJoinWorkflow"
    assert kwargs["id"] == "receipt-join-r1"


def test_pool_never_admits_inflows_or_transfers() -> None:
    # A same-magnitude refund (+$23.48) or a transfer must never be a
    # candidate: the exact shortcut runs with no model in the loop, and a
    # purchase receipt belongs to an outflow at a merchant.
    txns = (
        _txn("refund", "23.48"),  # inflow, exact magnitude
        _txn("xfer", "-23.48", payee="Transfer : Savings"),
        _txn("charge", "-23.48"),
    )
    pool = candidate_pool(_receipt(), txns)
    assert [str(t.ynab_id) for t in pool] == ["charge"]
    match = exact_single_match(_receipt(), pool)
    assert match is not None
    assert match.txn_id == "charge"


def test_fold_memo_directly_is_memo_only_and_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The settled-charge path writes ONLY the memo (category/approval/flag
    # untouched), verifies by re-read, and confirms on the receipt's thread.
    from ynab_agent.mail.client import MailClient
    from ynab_agent.ynab.client import YnabClient

    class _Ynab:
        def __init__(self) -> None:
            self.memo: str | None = "from the owner"
            self.patches: list[str] = []

        def snapshot(self, txn_id: str) -> YnabSnapshot:
            return _txn("t-9", "-23.48").model_copy(
                update={"memo": self.memo, "approved": True}
            )

        def patch_memo(self, txn_id: str, memo: str) -> None:
            self.patches.append(memo)
            self.memo = memo

    class _Mail:
        def __init__(self) -> None:
            self.sends: list[dict[str, object]] = []

        def send_on_thread(self, **kwargs: object) -> bool:
            self.sends.append(kwargs)
            return True

    ynab = _Ynab()
    mail = _Mail()
    monkeypatch.setenv("YNAB_AGENT_INBOX", "inbox-1")
    monkeypatch.setenv("YNAB_AGENT_OWNERS", "a@x.com")
    monkeypatch.setattr(YnabClient, "from_env", classmethod(lambda cls: ynab))
    monkeypatch.setattr(MailClient, "from_env", classmethod(lambda cls: mail))
    receipt = _receipt().model_copy(update={"source_thread_id": "thr-r"})
    asyncio.run(receipt_activities._fold_memo_directly("t-9", receipt))
    assert ynab.patches == ["from the owner · Whole Foods — $23.48"]
    assert len(mail.sends) == 1
    assert "Matched your receipt" in str(mail.sends[0]["body"])
    assert mail.sends[0]["seq_label"] == "yarcpt-matched-r1"
