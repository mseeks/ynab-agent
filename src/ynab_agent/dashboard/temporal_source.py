"""Temporal reader: the live run ledger (reuses the worker's client).

Reads the agent's durable state straight from Temporal: the poll heartbeat, the
in-flight W2 transactions counted by lifecycle state (each running workflow's
``state`` query), the rule registry's autonomy ladder (its ``view`` query), the
live autonomy offers, the recent inbound-dispatch tally, and any
terminated/failed workflows with their recovered reason. Everything is bounded
and best-effort: a failure returns whatever was gathered plus an error string,
never raising into the page.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Final

from temporalio.client import WorkflowExecutionStatus

from ynab_agent.dashboard.model import (
    DispatchTally,
    Failure,
    OfferRow,
    QueueItem,
    RuleRow,
    StateCount,
)
from ynab_agent.domain.base import Frozen

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from temporalio.client import Client, WorkflowExecution

_MAX_PER_TYPE: Final = 500
_MAX_STATE_QUERIES: Final = 200
_REGISTRY_ID: Final = "ynab-rule-registry"

# Operator-termination vocabulary: the human reasons attached to a deliberate
# go-live / re-test / reset wipe. Used to keep housekeeping out of the fault
# headline (the page still lists them under disclosure).
_RESET_MARKERS: Final = (
    "go-live",
    "go live",
    "reset",
    "re-test",
    "retest",
    "cleanup",
    "clean up",
    "scaffold",
    "teardown",
    "redeploy",
    "wipe",
)


def _intentional(kind: str, reason: str | None) -> bool:
    """Whether a terminal workflow was the operator's own housekeeping.

    Scoped to *terminated* runs carrying a human reason — a ``failed`` run is
    real breakage, never housekeeping — so a coincidental word in an exception
    message (e.g. "connection reset") is never miscounted as intentional.
    """
    if kind != "terminated" or not reason:
        return False
    low = reason.lower()
    return any(marker in low for marker in _RESET_MARKERS)


class TemporalReadout(Frozen):
    """The Temporal-derived pieces of the dashboard, ready to slot in."""

    poll_status: str = "none"
    poll_live: bool = False
    poll_last_start: datetime | None = None
    lifecycle_states: tuple[StateCount, ...] = ()
    in_flight: int = 0
    archived: int = 0
    terminated: int = 0
    rules: tuple[RuleRow, ...] = ()
    observe: int = 0
    eligible: int = 0
    blessed: int = 0
    offers: tuple[OfferRow, ...] = ()
    awaiting: tuple[QueueItem, ...] = ()
    dispatch: DispatchTally = DispatchTally()
    failures: tuple[Failure, ...] = ()


def _status(execution: WorkflowExecution) -> str:
    status = execution.status
    return status.name.lower() if status is not None else "unknown"


async def _take(
    iterator: AsyncIterator[WorkflowExecution], cap: int = _MAX_PER_TYPE
) -> AsyncIterator[WorkflowExecution]:
    """Yield at most ``cap`` executions (a runaway-visibility backstop)."""
    seen = 0
    async for execution in iterator:
        yield execution
        seen += 1
        if seen >= cap:
            return


async def _poll(client: Client) -> tuple[str, bool, datetime | None]:
    """The most recent poll tick: (status, live, last_start).

    Standard (SQL) Temporal visibility rejects an ``ORDER BY`` clause, so we
    take a bounded page and pick the latest start client-side rather than
    sorting in the query.
    """
    latest: WorkflowExecution | None = None
    async for execution in _take(
        client.list_workflows("WorkflowType = 'PollWorkflow'"), cap=100
    ):
        start = execution.start_time
        if latest is None or (
            start is not None
            and (latest.start_time is None or start > latest.start_time)
        ):
            latest = execution
    if latest is None:
        return "none", False, None
    live = latest.status in (
        WorkflowExecutionStatus.RUNNING,
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.CONTINUED_AS_NEW,
    )
    return _status(latest), live, latest.start_time


async def _lifecycle(
    client: Client,
) -> tuple[tuple[StateCount, ...], int, tuple[QueueItem, ...]]:
    """Count running W2s by state; collect the awaiting-human queue."""
    counts: dict[str, int] = {}
    awaiting: list[QueueItem] = []
    seen = 0
    async for execution in _take(
        client.list_workflows(
            "WorkflowType = 'TransactionWorkflow' "
            "AND ExecutionStatus = 'Running'"
        ),
        cap=_MAX_STATE_QUERIES,
    ):
        seen += 1
        try:
            handle = client.get_workflow_handle(
                execution.id, run_id=execution.run_id
            )
            state = await handle.query("state", result_type=str)
        except Exception:  # a busy/odd workflow shouldn't drop the whole panel
            state = "unknown"
        counts[state] = counts.get(state, 0) + 1
        if state == "awaiting_human":
            awaiting.append(
                QueueItem(
                    kind="proposal",
                    label=execution.id,
                    ident=execution.id,
                    since=execution.start_time,
                )
            )
    states = tuple(
        StateCount(state=name, count=counts[name]) for name in sorted(counts)
    )
    return states, seen, tuple(awaiting)


async def _registry(
    client: Client,
) -> tuple[tuple[RuleRow, ...], int, int, int]:
    """The rule table + (observe, eligible, blessed) counts (view query)."""
    from ynab_agent.domain.allocations import ProposedCategory
    from ynab_agent.domain.enums import RuleSource, TrustState
    from ynab_agent.workflow.registry_types import RegistryView

    handle = client.get_workflow_handle(_REGISTRY_ID)
    view = await handle.query("view", result_type=RegistryView)
    rows: list[RuleRow] = []
    observe = eligible = blessed = 0
    for rule in view.rules:
        allocation = rule.action.allocation
        category = (
            str(allocation.category)
            if isinstance(allocation, ProposedCategory)
            else "split"
        )
        rows.append(
            RuleRow(
                payee=rule.match.payee_pattern,
                category=category,
                trust=rule.trust.value,
                source=rule.source.value,
                hits=rule.hits,
                offered=rule.offered_at is not None,
                last_confirmed_at=rule.last_confirmed_at,
            )
        )
        if rule.source is RuleSource.HUMAN_EXPLICIT:
            blessed += 1
        elif rule.trust is TrustState.TRUSTED:
            eligible += 1
        else:
            observe += 1
    return tuple(rows), observe, eligible, blessed


async def _offers(client: Client) -> tuple[OfferRow, ...]:
    """The live autonomy-offer workflows (awaiting the owner's yes/no)."""
    offers: list[OfferRow] = []
    async for execution in _take(
        client.list_workflows(
            "WorkflowType = 'AutonomyOfferWorkflow' "
            "AND ExecutionStatus = 'Running'"
        )
    ):
        offers.append(
            OfferRow(
                rule_id=execution.id.removeprefix("autonomy-offer-"),
                payee="",
                status=_status(execution),
                started_at=execution.start_time,
            )
        )
    return tuple(offers)


async def _dispatch(client: Client) -> DispatchTally:
    """Tally recent inbound dispatch results by routing action."""
    counts: dict[str, int] = {}
    total = 0
    async for execution in _take(
        client.list_workflows(
            "WorkflowType = 'DispatchWorkflow' "
            "AND ExecutionStatus = 'Completed'"
        )
    ):
        total += 1
        action = "?"
        try:
            handle = client.get_workflow_handle(
                execution.id, run_id=execution.run_id
            )
            result = await handle.result()
            got = getattr(result, "action", None)
            if isinstance(got, str):
                action = got
        except Exception:  # a bad decode shouldn't drop the tally
            action = "?"
        counts[action] = counts.get(action, 0) + 1
    return DispatchTally(
        transaction=counts.get("transaction", 0),
        offer=counts.get("offer", 0),
        receipt=counts.get("receipt", 0),
        command=counts.get("command", 0),
        quarantine=counts.get("quarantine", 0),
        ignore=counts.get("ignore", 0),
        total=total,
    )


async def _reason(client: Client, execution: WorkflowExecution) -> str | None:
    """Recover a terminated/failed workflow's human reason from history."""
    try:
        handle = client.get_workflow_handle(
            execution.id, run_id=execution.run_id
        )
        found: str | None = None
        async for event in handle.fetch_history_events():
            terminated = event.workflow_execution_terminated_event_attributes
            if terminated.reason:
                found = terminated.reason
            failed = event.workflow_execution_failed_event_attributes
            if failed.failure.message:
                found = failed.failure.message
        return found
    except Exception:  # the reason is a nicety — never fail the panel for it
        return None


async def _terminal(client: Client) -> tuple[int, int, tuple[Failure, ...]]:
    """Recent archived (completed W2) + terminated/failed counts + failures."""
    archived = terminated = 0
    failures: list[Failure] = []
    async for _completed in _take(
        client.list_workflows(
            "WorkflowType = 'TransactionWorkflow' "
            "AND ExecutionStatus = 'Completed'"
        )
    ):
        archived += 1
    async for execution in _take(
        client.list_workflows(
            "ExecutionStatus = 'Terminated' OR ExecutionStatus = 'Failed'"
        )
    ):
        terminated += 1
        if len(failures) < 25:
            kind = _status(execution)
            reason = await _reason(client, execution)
            failures.append(
                Failure(
                    workflow_id=execution.id,
                    kind=kind,
                    reason=reason,
                    when=execution.close_time,
                    intentional=_intentional(kind, reason),
                )
            )
    return archived, terminated, tuple(failures)


async def fetch(client: Client) -> tuple[TemporalReadout, str | None]:
    """Read the agent's Temporal state; each panel degrades independently.

    Every sub-read is guarded on its own, so one unsupported visibility query
    (or a transient failure) reddens only its panel and names itself in the
    error — the rest of the page still fills in.
    """
    poll_status = "none"
    poll_live = False
    poll_last: datetime | None = None
    states: tuple[StateCount, ...] = ()
    in_flight = 0
    awaiting: tuple[QueueItem, ...] = ()
    rules: tuple[RuleRow, ...] = ()
    observe = eligible = blessed = 0
    offers: tuple[OfferRow, ...] = ()
    dispatch = DispatchTally()
    archived = terminated = 0
    failures: tuple[Failure, ...] = ()
    errors: list[str] = []

    try:
        poll_status, poll_live, poll_last = await _poll(client)
    except Exception as exc:
        errors.append(f"poll: {type(exc).__name__}")
    try:
        states, in_flight, awaiting = await _lifecycle(client)
    except Exception as exc:
        errors.append(f"lifecycle: {type(exc).__name__}")
    # The registry may not exist until the first learning signal — a normal
    # state, not an error, so its absence is simply suppressed.
    with contextlib.suppress(Exception):
        rules, observe, eligible, blessed = await _registry(client)
    try:
        offers = await _offers(client)
    except Exception as exc:
        errors.append(f"offers: {type(exc).__name__}")
    try:
        dispatch = await _dispatch(client)
    except Exception as exc:
        errors.append(f"dispatch: {type(exc).__name__}")
    try:
        archived, terminated, failures = await _terminal(client)
    except Exception as exc:
        errors.append(f"terminal: {type(exc).__name__}")

    readout = TemporalReadout(
        poll_status=poll_status,
        poll_live=poll_live,
        poll_last_start=poll_last,
        lifecycle_states=states,
        in_flight=in_flight,
        archived=archived,
        terminated=terminated,
        rules=rules,
        observe=observe,
        eligible=eligible,
        blessed=blessed,
        offers=offers,
        awaiting=awaiting,
        dispatch=dispatch,
        failures=failures,
    )
    return readout, "; ".join(errors) if errors else None
