"""Tests for the W3 dispatch activities (SPEC §5).

``resolve_thread`` maps an AgentMail thread id back to its transaction via a
Temporal visibility query on the ``TxnThreadId`` search attribute (store-free,
SPEC §0.5); ``signal_transaction`` turns a verified reply into a
``submit_inbound`` signal-with-start on that W2. Both are exercised against a
fake client injected as the cached connection; the live calls are covered by
the worker against a real Temporal server.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.ids import MessageId, ThreadId
from ynab_agent.workflow import dispatch_activities, temporal_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ynab_agent.domain.receipt import Receipt


class _FakeExecution:
    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id


class _FakeClient:
    """Stands in for the Temporal client's ``list_workflows`` visibility API."""

    def __init__(self, executions: list[_FakeExecution]) -> None:
        self._executions = executions
        self.queries: list[str] = []

    def list_workflows(self, query: str) -> AsyncIterator[_FakeExecution]:
        self.queries.append(query)

        async def _gen() -> AsyncIterator[_FakeExecution]:
            for execution in self._executions:
                yield execution

        return _gen()


def test_resolve_thread_returns_matching_workflow_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([_FakeExecution("txn-123")])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    result = asyncio.run(dispatch_activities.resolve_thread("thread-abc"))
    assert result == "txn-123"
    assert fake.queries == ['TxnThreadId = "thread-abc"']


def test_resolve_thread_none_when_no_workflow_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    assert asyncio.run(dispatch_activities.resolve_thread("orphan")) is None


def test_resolve_thread_none_for_none_input() -> None:
    # No client is touched when there is no thread id to resolve.
    assert asyncio.run(dispatch_activities.resolve_thread(None)) is None


def test_resolve_thread_escapes_quotes_in_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([_FakeExecution("txn-9")])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(dispatch_activities.resolve_thread('th"read'))
    assert fake.queries == ['TxnThreadId = "th\\"read"']


def test_resolve_offer_thread_query_filters_to_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([_FakeExecution("autonomy-offer-r1")])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    result = asyncio.run(dispatch_activities.resolve_offer_thread("thread-abc"))
    assert result == "autonomy-offer-r1"
    # A closed offer must not be resurrected, so the query is Running-only.
    assert fake.queries == [
        'OfferThreadId = "thread-abc" AND ExecutionStatus = "Running"'
    ]


def test_resolve_offer_thread_none_for_none_input() -> None:
    assert asyncio.run(dispatch_activities.resolve_offer_thread(None)) is None


class _FakeHandle:
    def __init__(self) -> None:
        self.signals: list[tuple[str, object]] = []

    async def signal(self, name: str, arg: object) -> None:
        self.signals.append((name, arg))


class _FakeHandleClient:
    """Stands in for ``get_workflow_handle`` + ``signal``."""

    def __init__(self) -> None:
        self.handle = _FakeHandle()
        self.requested: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _FakeHandle:
        self.requested.append(workflow_id)
        return self.handle


def test_signal_offer_signals_the_offer_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHandleClient()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(
        dispatch_activities.signal_offer(
            "autonomy-offer-r1", _message(thread_id="t")
        )
    )
    assert fake.requested == ["autonomy-offer-r1"]
    assert len(fake.handle.signals) == 1
    name, arg = fake.handle.signals[0]
    assert name == "submit_response"
    assert arg.body == "actually make it Dining"  # type: ignore[attr-defined]


class _FakeStartClient:
    """Captures ``start_workflow`` (signal-with-start) calls."""

    def __init__(self) -> None:
        self.started: list[tuple[object, object, dict[str, object]]] = []

    async def start_workflow(
        self, workflow: object, arg: object, **kwargs: object
    ) -> None:
        self.started.append((workflow, arg, kwargs))


def _message(*, thread_id: str | None) -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address="owner@example.com",
        subject="re: coffee",
        body="actually make it Dining",
        thread_id=ThreadId(thread_id) if thread_id is not None else None,
        signature_verified=True,
    )


def test_signal_transaction_signals_with_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStartClient()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(
        dispatch_activities.signal_transaction("txn-1", _message(thread_id="t"))
    )
    assert len(fake.started) == 1
    workflow, _arg, kwargs = fake.started[0]
    assert workflow == "TransactionWorkflow"
    assert kwargs["id"] == "txn-1"
    assert kwargs["start_signal"] == "submit_inbound"
    reply = kwargs["start_signal_args"][0]  # type: ignore[index]
    assert reply.text == "actually make it Dining"
    assert reply.thread_id == "t"


def test_signal_transaction_requires_a_thread_id() -> None:
    with pytest.raises(RuntimeError, match="no thread id"):
        asyncio.run(
            dispatch_activities.signal_transaction(
                "txn-1", _message(thread_id=None)
            )
        )


# ── handle_command: read-back + confirm before blessing (SPEC §5c, §0.6) ─────
class _FakeYnab:
    def category_spends(self) -> tuple[object, ...]:
        from ynab_agent.budget.overspend import CategorySpend
        from ynab_agent.domain.ids import CategoryId
        from ynab_agent.domain.money import Money

        zero = Money.from_milliunits(0)
        return (
            CategorySpend(
                category=CategoryId("groceries"),
                name="Groceries",
                budgeted=zero,
                activity=zero,
                balance=zero,
            ),
        )


def _stub_command(monkeypatch: pytest.MonkeyPatch, *, bless: bool) -> None:
    """Stub the YNAB read and the model parse so handle_command is offline."""
    from ynab_agent.agentic import command as command_mod
    from ynab_agent.ynab.client import YnabClient

    monkeypatch.setattr(
        YnabClient, "from_env", classmethod(lambda cls: _FakeYnab())
    )

    async def _parse(request: object) -> command_mod.CommandReading:
        kind = (
            command_mod.CommandKind.BLESS
            if bless
            else command_mod.CommandKind.OTHER
        )
        return command_mod.CommandReading(
            kind=kind,
            payee_pattern="Costco" if bless else None,
            category_id="groceries" if bless else None,
        )

    monkeypatch.setattr(command_mod, "parse_command", _parse)


def test_handle_command_opens_a_confirm_not_an_inline_bless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A standing command no longer blesses inline (SPEC §0.6): it starts a
    # CommandConfirmWorkflow read-back, never signals the registry directly.
    _stub_command(monkeypatch, bless=True)
    fake = _FakeStartClient()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)

    asyncio.run(dispatch_activities.handle_command(_message(thread_id="t")))

    assert len(fake.started) == 1
    workflow, arg, kwargs = fake.started[0]
    # Crucially the confirm workflow starts — not a direct registry bless.
    assert workflow == "CommandConfirmWorkflow"
    assert str(kwargs["id"]).startswith("command-confirm-")
    assert arg.command.match.payee_pattern == "Costco"  # type: ignore[attr-defined]


def test_handle_command_ignores_a_non_bless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_command(monkeypatch, bless=False)
    fake = _FakeStartClient()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)

    asyncio.run(dispatch_activities.handle_command(_message(thread_id="t")))

    assert fake.started == []


# ── handle_command: revoke / list rules / help (SPEC §14.5) ──────────────────
def _stub_parse(monkeypatch: pytest.MonkeyPatch, reading: object) -> None:
    """Stub the YNAB read and the model parse with a fixed reading."""
    from ynab_agent.agentic import command as command_mod
    from ynab_agent.ynab.client import YnabClient

    monkeypatch.setattr(
        YnabClient, "from_env", classmethod(lambda cls: _FakeYnab())
    )

    async def _parse(request: object) -> object:
        return reading

    monkeypatch.setattr(command_mod, "parse_command", _parse)


def _blessed_view(payee: str, category_id: str) -> object:
    from ynab_agent.domain.allocations import ProposedCategory
    from ynab_agent.domain.enums import RuleSource, TrustState
    from ynab_agent.domain.ids import CategoryId, RuleId
    from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
    from ynab_agent.workflow.registry_types import RegistryView

    rule = Rule(
        id=RuleId("r1"),
        match=RuleMatch(payee_pattern=payee),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId(category_id))
        ),
        trust=TrustState.TRUSTED,
        source=RuleSource.HUMAN_EXPLICIT,
    )
    return RegistryView(rules=(rule,), eligible=())


class _FakeRegistryHandle:
    """A registry handle whose ``view`` query returns a view or 'not found'."""

    def __init__(self, view: object | None) -> None:
        self._view = view

    async def query(self, name: str, result_type: object = None) -> object:
        from temporalio.service import RPCError, RPCStatusCode

        if self._view is None:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return self._view


class _FakeRegistryClient(_FakeStartClient):
    """start_workflow capture + a registry handle for the view query."""

    def __init__(self, view: object | None) -> None:
        super().__init__()
        self._handle = _FakeRegistryHandle(view)

    def get_workflow_handle(self, workflow_id: str) -> _FakeRegistryHandle:
        return self._handle


def _kind(name: str) -> object:
    from ynab_agent.agentic.command import CommandKind, CommandReading

    return CommandReading(
        kind=CommandKind(name),
        payee_pattern="Costco" if name == "revoke" else None,
    )


def test_revoke_signals_the_registry_and_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_parse(monkeypatch, _kind("revoke"))
    mail = _stub_mail(monkeypatch)
    fake = _FakeRegistryClient(view=_blessed_view("Costco", "groceries"))
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)

    asyncio.run(dispatch_activities.handle_command(_message(thread_id="t")))

    assert len(fake.started) == 1
    workflow, _arg, kwargs = fake.started[0]
    assert workflow == "RuleRegistryWorkflow"
    assert kwargs["start_signal"] == "revoke"
    assert kwargs["start_signal_args"] == ["Costco"]
    assert len(mail.sends) == 1
    assert "stopped auto-handling Costco" in str(mail.sends[0]["body"])


def test_revoke_with_nothing_blessed_replies_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No registry yet (or no blessed rule for the payee): say so, signal
    # nothing — never a fake "done".
    _stub_parse(monkeypatch, _kind("revoke"))
    mail = _stub_mail(monkeypatch)
    fake = _FakeRegistryClient(view=None)
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)

    asyncio.run(dispatch_activities.handle_command(_message(thread_id="t")))

    assert fake.started == []
    assert len(mail.sends) == 1
    assert "not auto-handling Costco" in str(mail.sends[0]["body"])


def test_list_rules_replies_with_the_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_parse(monkeypatch, _kind("list_rules"))
    mail = _stub_mail(monkeypatch)
    fake = _FakeRegistryClient(view=_blessed_view("Costco", "groceries"))
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)

    asyncio.run(dispatch_activities.handle_command(_message(thread_id="t")))

    assert fake.started == []  # a read, never a write
    body = str(mail.sends[0]["body"])
    assert "Costco → Groceries" in body  # the category by NAME, not id
    assert "Auto-handled" in body


def test_help_replies_with_the_capability_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_parse(monkeypatch, _kind("help"))
    mail = _stub_mail(monkeypatch)
    fake = _FakeRegistryClient(view=None)
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)

    asyncio.run(dispatch_activities.handle_command(_message(thread_id="t")))

    assert fake.started == []
    body = str(mail.sends[0]["body"])
    assert "always categorize" in body
    assert "stop auto-handling" in body


# ── route_receipt: acknowledge instead of swallowing (SPEC §5b, §6) ──────────
class _FakeMail:
    def __init__(self) -> None:
        self.sends: list[dict[str, object]] = []

    def send_on_thread(
        self,
        *,
        inbox_id: str,
        thread_id: str,
        body: str,
        seq_label: str,
        to: list[str] | None = None,
        html: str | None = None,
    ) -> bool:
        self.sends.append(
            {
                "thread_id": thread_id,
                "body": body,
                "seq_label": seq_label,
                "html": html,
            }
        )
        return True


def _stub_mail(monkeypatch: pytest.MonkeyPatch) -> _FakeMail:
    from ynab_agent.mail.client import MailClient

    monkeypatch.setenv("YNAB_AGENT_INBOX", "inbox-1")
    monkeypatch.setenv("YNAB_AGENT_OWNERS", "matthew@x.com,wife@x.com")
    fake = _FakeMail()
    monkeypatch.setattr(MailClient, "from_env", classmethod(lambda cls: fake))
    return fake


def _stub_receipt_pipeline(
    monkeypatch: pytest.MonkeyPatch, *, parsed: object
) -> tuple[list[Receipt], list[Receipt]]:
    """Stub the model parse and the park/start helpers (offline)."""
    from ynab_agent.agentic import receipt_parse
    from ynab_agent.workflow import receipt_activities

    async def _parse(request: object, *, model: object = None) -> object:
        return parsed

    monkeypatch.setattr(receipt_parse, "parse_receipt", _parse)
    parked: list[Receipt] = []
    started: list[Receipt] = []

    async def _park(receipt: Receipt) -> None:
        parked.append(receipt)

    async def _start(receipt: Receipt) -> None:
        started.append(receipt)

    monkeypatch.setattr(receipt_activities, "park_in_ledger", _park)
    monkeypatch.setattr(receipt_activities, "start_join", _start)
    return parked, started


def test_route_receipt_parses_parks_acks_and_starts_the_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The W4 entry point (SPEC §6): a parseable forward is parked in the
    # ledger, acknowledged naming what was read, and a join attempt starts.
    from ynab_agent.agentic.receipt_parse import ParsedReceipt

    fake = _stub_mail(monkeypatch)
    parked, started = _stub_receipt_pipeline(
        monkeypatch,
        parsed=ParsedReceipt(
            is_receipt=True, merchant="Whole Foods", total="$23.48"
        ),
    )
    asyncio.run(dispatch_activities.route_receipt(_message(thread_id="thr-r")))
    assert len(parked) == 1 and len(started) == 1
    receipt = parked[0]
    assert str(receipt.id) == "m1"  # the message id is the dedup key
    assert str(receipt.source_thread_id) == "thr-r"
    assert len(fake.sends) == 1
    sent = fake.sends[0]
    assert "Whole Foods — $23.48" in str(sent["body"])  # names what was read
    assert sent["seq_label"] == "yarcpt-ack-m1"  # deduped on the message id


def test_route_receipt_answers_honestly_when_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A forward that isn't a receipt is never parked as junk: say so, and
    # point at the path that always works.
    from ynab_agent.agentic.receipt_parse import ParsedReceipt

    fake = _stub_mail(monkeypatch)
    parked, started = _stub_receipt_pipeline(
        monkeypatch, parsed=ParsedReceipt(is_receipt=False)
    )
    asyncio.run(dispatch_activities.route_receipt(_message(thread_id="thr-r")))
    assert parked == [] and started == []
    assert len(fake.sends) == 1
    assert "didn't look like a purchase receipt" in str(fake.sends[0]["body"])
    assert fake.sends[0]["seq_label"] == "yarcpt-unread-m1"


def test_route_receipt_no_op_without_a_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ynab_agent.agentic.receipt_parse import ParsedReceipt

    fake = _stub_mail(monkeypatch)
    _stub_receipt_pipeline(
        monkeypatch, parsed=ParsedReceipt(is_receipt=True, merchant="X")
    )
    asyncio.run(dispatch_activities.route_receipt(_message(thread_id=None)))
    assert fake.sends == []
