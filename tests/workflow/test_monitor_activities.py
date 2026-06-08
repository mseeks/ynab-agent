"""The W6 monitor activities against a real ledger (the dedupe seam).

The monitor workflow's own test mocks these activities, so the seam they cross —
``save_alert`` signalling and ``load_prior_alert`` querying the durable
``OverspendLedgerWorkflow`` over the *pydantic* data converter — is only
exercised here, mirroring ``test_load_payee_rules`` + ``test_failure_alert``.
The hydration is load-bearing: the query decodes to a plain ``dict``, so
``load_prior_alert`` rebuilds a real ``PriorAlert`` (``should_alert`` reads
attributes off it — the #4 dict-vs-object bug); ``isinstance`` pins that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import ynab_agent.workflow.temporal_client as temporal_client
from ynab_agent.budget.overspend import OverspendVerdict, PriorAlert
from ynab_agent.domain.money import Money
from ynab_agent.workflow import monitor_activities
from ynab_agent.workflow.overspend_ledger_types import (
    OVERSPEND_LEDGER_WORKFLOW_ID,
    LedgerParams,
)
from ynab_agent.workflow.overspend_ledger_workflow import (
    OverspendLedgerWorkflow,
)
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    import pytest

_TASK_QUEUE = "overspend-activities-test"
# The period is supplied by the caller (the workflow in production); save and
# load must agree on it for the ledger round-trip, as the workflow's single
# per-pass value guarantees.
_PERIOD = "2026-06"

# The ledger is a long-lived singleton — in production it is already running
# (started on the first-ever alert) when a later daily pass loads from it. The
# round-trip tests pre-start its run to model that: ``save_alert`` then signals
# the running instance (USE_EXISTING), so the query reads the recorded state.
# (A signal-with-start followed by an immediate same-process query races on the
# time-skipping server; in production save and load are a day apart.)


async def test_save_then_load_round_trips_real_prior_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``save_alert`` creates the ledger; ``load_prior_alert`` reads it back."""
    alert = PriorAlert(
        verdict=OverspendVerdict.ALREADY_OVER,
        projected=Money.from_currency("520"),
    )
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", _TASK_QUEUE)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[OverspendLedgerWorkflow],
        ),
    ):
        monkeypatch.setattr(temporal_client, "_CLIENT", env.client)
        await env.client.start_workflow(
            OverspendLedgerWorkflow.run,
            LedgerParams(),
            id=OVERSPEND_LEDGER_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
        )
        await monitor_activities.save_alert("dining", alert, _PERIOD)
        loaded = await monitor_activities.load_prior_alert("dining", _PERIOD)

    assert loaded == alert
    assert isinstance(loaded, PriorAlert)


async def test_load_prior_alert_is_none_for_unalerted_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert = PriorAlert(
        verdict=OverspendVerdict.TRENDING_OVER,
        projected=Money.from_currency("500"),
    )
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", _TASK_QUEUE)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[OverspendLedgerWorkflow],
        ),
    ):
        monkeypatch.setattr(temporal_client, "_CLIENT", env.client)
        await env.client.start_workflow(
            OverspendLedgerWorkflow.run,
            LedgerParams(),
            id=OVERSPEND_LEDGER_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
        )
        await monitor_activities.save_alert("dining", alert, _PERIOD)
        # The alerted category reads back; an unalerted one is independent.
        assert (
            await monitor_activities.load_prior_alert("dining", _PERIOD)
            is not None
        )
        assert await monitor_activities.load_prior_alert("gas", _PERIOD) is None


async def test_load_prior_alert_none_when_ledger_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query before any alert (the singleton never started) → ``None``."""
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", _TASK_QUEUE)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[OverspendLedgerWorkflow],
        ),
    ):
        monkeypatch.setattr(temporal_client, "_CLIENT", env.client)
        assert (
            await monitor_activities.load_prior_alert("dining", _PERIOD) is None
        )
