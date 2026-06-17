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

from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

import ynab_agent.workflow.temporal_client as temporal_client
from ynab_agent.budget.overspend import (
    CategorySpend,
    MonthClock,
    OverspendVerdict,
    PriorAlert,
)
from ynab_agent.domain.ids import CategoryId
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
from ynab_agent.ynab.client import YnabClient

if TYPE_CHECKING:
    import datetime

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


class _FakeClient:
    """A stand-in YNAB client for the fetch activity (no real API)."""

    def __init__(
        self,
        spends: list[CategorySpend],
        scheduled: dict[CategoryId, Money] | None = None,
        *,
        scheduled_error: bool = False,
    ) -> None:
        self._spends = spends
        self._scheduled = scheduled or {}
        self._scheduled_error = scheduled_error

    def category_spends(self) -> tuple[CategorySpend, ...]:
        return tuple(self._spends)

    def scheduled_outflows(
        self, today: datetime.date, month_end: datetime.date
    ) -> dict[CategoryId, Money]:
        if self._scheduled_error:
            msg = "scheduled fetch failed"
            raise RuntimeError(msg)
        return self._scheduled


def _bare_spend(name: str) -> CategorySpend:
    return CategorySpend(
        category=CategoryId(name),
        name=name.title(),
        budgeted=Money.from_currency("400"),
        activity=Money.from_currency("-100"),
        balance=Money.from_currency("300"),
    )


_FETCH_CLOCK = MonthClock(day_of_month=17, days_in_month=30)


async def test_fetch_category_spends_merges_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(
        [_bare_spend("rent"), _bare_spend("dining")],
        {CategoryId("rent"): Money.from_currency("1200")},
    )
    monkeypatch.setattr(YnabClient, "from_env", staticmethod(lambda: fake))
    spends = await ActivityEnvironment().run(
        monitor_activities.fetch_category_spends, "2026-06", _FETCH_CLOCK
    )
    by_id = {str(s.category): s for s in spends}
    assert by_id["rent"].scheduled_remaining == Money.from_currency("1200")
    assert by_id["dining"].scheduled_remaining.is_zero  # nothing scheduled


async def test_fetch_category_spends_degrades_when_scheduled_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A scheduled-transactions outage must not crash the pass: every category
    # falls back to scheduled_remaining = 0 (the plain run-rate projection).
    fake = _FakeClient([_bare_spend("rent")], scheduled_error=True)
    monkeypatch.setattr(YnabClient, "from_env", staticmethod(lambda: fake))
    spends = await ActivityEnvironment().run(
        monitor_activities.fetch_category_spends, "2026-06", _FETCH_CLOCK
    )
    assert spends[0].scheduled_remaining.is_zero
