"""The Temporal reader derives the run ledger and degrades to an error."""

from __future__ import annotations

import asyncio
import datetime
import types
from typing import TYPE_CHECKING

from temporalio.client import WorkflowExecutionStatus

from ynab_agent.dashboard import temporal_source
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.enums import RuleSource, TrustState
from ynab_agent.domain.ids import CategoryId, RuleId
from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
from ynab_agent.workflow.registry_types import RegistryView

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_NOW = datetime.datetime(2026, 6, 5, 12, 0, tzinfo=datetime.UTC)
_S = WorkflowExecutionStatus


def _rule(rid: str, trust: TrustState, source: RuleSource) -> Rule:
    return Rule(
        id=RuleId(rid),
        match=RuleMatch(payee_pattern=rid),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("c"))
        ),
        trust=trust,
        source=source,
    )


class _Exec:
    def __init__(
        self,
        wid: str,
        status: WorkflowExecutionStatus,
        *,
        close: datetime.datetime | None = None,
    ) -> None:
        self.id = wid
        self.run_id = "run"
        self.status = status
        self.start_time = _NOW
        self.close_time = close


class _Handle:
    def __init__(
        self,
        *,
        state: str | None = None,
        view: object = None,
        result: object = None,
    ) -> None:
        self._state = state
        self._view = view
        self._result = result

    async def query(self, name: str, result_type: object = None) -> object:
        if name == "state":
            return self._state
        if name == "view":
            return self._view
        msg = name
        raise KeyError(msg)

    async def result(self) -> object:
        return self._result

    async def fetch_history_events(self) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - makes this an async generator


class _Client:
    def __init__(
        self,
        routes: list[tuple[str, list[_Exec]]],
        handles: dict[str, _Handle],
        *,
        raise_on_list: bool = False,
    ) -> None:
        self._routes = routes
        self._handles = handles
        self._raise = raise_on_list

    def list_workflows(self, query: str) -> AsyncIterator[_Exec]:
        if self._raise:
            msg = "visibility down"
            raise RuntimeError(msg)
        execs: list[_Exec] = []
        for needle, found in self._routes:
            if needle in query:
                execs = found
                break

        async def _gen() -> AsyncIterator[_Exec]:
            for execution in execs:
                yield execution

        return _gen()

    def get_workflow_handle(
        self, workflow_id: str, run_id: str | None = None
    ) -> _Handle:
        return self._handles.get(workflow_id, _Handle())


def _client() -> _Client:
    view = RegistryView(
        rules=(
            _rule("blessed", TrustState.TRUSTED, RuleSource.HUMAN_EXPLICIT),
            _rule("eligible", TrustState.TRUSTED, RuleSource.LEARNED),
            _rule("observe", TrustState.SUGGESTED, RuleSource.LEARNED),
        ),
        eligible=(_rule("eligible", TrustState.TRUSTED, RuleSource.LEARNED),),
    )
    routes = [
        ("PollWorkflow", [_Exec("poll-1", _S.COMPLETED)]),
        (
            "TransactionWorkflow' AND ExecutionStatus = 'Running'",
            [_Exec("t1", _S.RUNNING), _Exec("t2", _S.RUNNING)],
        ),
        ("AutonomyOfferWorkflow", [_Exec("autonomy-offer-r1", _S.RUNNING)]),
        ("DispatchWorkflow", [_Exec("d1", _S.COMPLETED)]),
        (
            "TransactionWorkflow' AND ExecutionStatus = 'Completed'",
            [_Exec("a1", _S.COMPLETED), _Exec("a2", _S.COMPLETED)],
        ),
        ("Terminated", [_Exec("f1", _S.TERMINATED, close=_NOW)]),
    ]
    handles = {
        "t1": _Handle(state="enriching"),
        "t2": _Handle(state="awaiting_human"),
        "d1": _Handle(result=types.SimpleNamespace(action="transaction")),
        temporal_source._REGISTRY_ID: _Handle(view=view),
    }
    return _Client(routes, handles)


def test_fetch_derives_the_whole_ledger() -> None:
    readout, error = asyncio.run(temporal_source.fetch(_client()))  # type: ignore[arg-type]
    assert error is None
    assert readout.poll_live is True
    states = {s.state: s.count for s in readout.lifecycle_states}
    assert states == {"enriching": 1, "awaiting_human": 1}
    assert readout.in_flight == 2
    assert len(readout.awaiting) == 1  # the awaiting_human txn
    assert readout.observe == 1
    assert readout.eligible == 1
    assert readout.blessed == 1
    assert len(readout.offers) == 1
    assert readout.offers[0].rule_id == "r1"
    assert readout.dispatch.transaction == 1
    assert readout.dispatch.total == 1
    assert readout.archived == 2
    assert readout.terminated == 1
    assert len(readout.failures) == 1


def test_fetch_degrades_to_an_error_when_visibility_is_down() -> None:
    client = _Client([], {}, raise_on_list=True)
    readout, error = asyncio.run(temporal_source.fetch(client))  # type: ignore[arg-type]
    assert error is not None
    assert "RuntimeError" in error
    assert readout.poll_live is False
