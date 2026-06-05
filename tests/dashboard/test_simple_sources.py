"""Source readers: pure helpers + the graceful-degradation 'off' paths."""

from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING

from ynab_agent.dashboard import (
    agentmail_source,
    clickhouse_source,
    github_source,
    ynab_source,
)

if TYPE_CHECKING:
    import pytest

_CH_ENV = ("YNAB_AGENT_CLICKHOUSE_URL",)
_GH_ENV = ("YNAB_AGENT_GITHUB_TOKEN",)
_MAIL_ENV = ("AGENTMAIL_API_KEY", "YNAB_AGENT_INBOX")


# ── ClickHouse ───────────────────────────────────────────────────────────────
def test_clickhouse_scalar_coercion() -> None:
    assert clickhouse_source._int("42") == 42
    assert (
        clickhouse_source._int("99") == 99
    )  # JSON quotes a UInt64 as a string
    assert clickhouse_source._int("nope") == 0
    assert clickhouse_source._float("1.5") == 1.5
    assert clickhouse_source._float(None) == 0.0


def test_clickhouse_datetime_parsing() -> None:
    parsed = clickhouse_source._ch_datetime("2026-06-05 12:00:00")
    assert parsed is not None
    assert parsed.tzinfo is datetime.UTC
    assert clickhouse_source._ch_datetime("1970-01-01 00:00:00") is None
    assert clickhouse_source._ch_datetime("") is None


def test_clickhouse_off_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _CH_ENV:
        monkeypatch.delenv(name, raising=False)
    telemetry, error = asyncio.run(clickhouse_source.fetch())
    assert error == "off"
    assert telemetry.available is False


# ── GitHub ───────────────────────────────────────────────────────────────────
def test_github_ci_reduction() -> None:
    assert github_source._ci_of(["success", "success"]) == "passed"
    assert github_source._ci_of(["success", "failure"]) == "failed"
    assert github_source._ci_of(["success", None]) == "running"
    assert github_source._ci_of([]) is None


def test_github_state() -> None:
    assert github_source._state({"merged_at": "x"}) == "merged"
    assert github_source._state({"state": "open"}) == "open"
    assert github_source._state({"state": "closed"}) == "closed"


def test_github_off_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GH_ENV:
        monkeypatch.delenv(name, raising=False)
    deploy, error = asyncio.run(github_source.fetch())
    assert error == "off"
    assert deploy.prs == ()


# ── AgentMail ────────────────────────────────────────────────────────────────
def test_agentmail_kind_from_labels() -> None:
    assert agentmail_source._kind(("yaoffer-r1",)) == "offer"
    assert agentmail_source._kind(("ynab-agent", "yatxn-t1")) == "proposal"
    assert agentmail_source._kind(("ynab-agent",)) == "thread"


def test_agentmail_off_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _MAIL_ENV:
        monkeypatch.delenv(name, raising=False)
    conversations, error = asyncio.run(agentmail_source.fetch())
    assert error == "off"
    assert conversations == ()


# ── YNAB ─────────────────────────────────────────────────────────────────────
class _FakeYnab:
    def unapproved(self) -> tuple[object, ...]:
        from ynab_agent.domain.ids import AccountId, YnabTransactionId
        from ynab_agent.domain.money import Money
        from ynab_agent.domain.transaction import YnabSnapshot

        return (
            YnabSnapshot(
                ynab_id=YnabTransactionId("t1"),
                account=AccountId("a1"),
                payee="Blue Bottle",
                amount=Money.from_currency("-4.50"),
                txn_date=datetime.date(2026, 6, 1),
            ),
        )

    def category_spends(self) -> tuple[object, ...]:
        from ynab_agent.budget.overspend import CategorySpend
        from ynab_agent.domain.ids import CategoryId
        from ynab_agent.domain.money import Money

        return (
            CategorySpend(
                category=CategoryId("dining"),
                name="Dining",
                budgeted=Money.from_currency("50"),
                activity=Money.from_currency("-62"),
                balance=Money.from_currency("-12"),
            ),
            CategorySpend(
                category=CategoryId("gas"),
                name="Gas",
                budgeted=Money.from_currency("40"),
                activity=Money.from_currency("-10"),
                balance=Money.from_currency("30"),
            ),
        )


def test_ynab_reads_backlog_and_overspent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ynab_agent.ynab.client import YnabClient

    monkeypatch.setattr(
        YnabClient, "from_env", classmethod(lambda cls: _FakeYnab())
    )
    budget, error = asyncio.run(ynab_source.fetch())
    assert error is None
    assert budget.available is True
    assert budget.unapproved == 1
    assert budget.unapproved_sample[0].payee == "Blue Bottle"
    # Only the negative-balance category is overspent.
    assert [c.name for c in budget.overspent] == ["Dining"]


def test_ynab_off_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from ynab_agent.ynab.client import YnabClient

    def _raise(cls: type) -> _FakeYnab:
        msg = "YNAB_API_KEY is not set"
        raise RuntimeError(msg)

    monkeypatch.setattr(YnabClient, "from_env", classmethod(_raise))
    budget, error = asyncio.run(ynab_source.fetch())
    assert error == "off"
    assert budget.available is False
