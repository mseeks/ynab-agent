"""Tests for the append-only audit log mechanics (SPEC §9)."""

from __future__ import annotations

import datetime

from ynab_agent.audit.entry import AuditLog, MessageSent, StateChanged
from ynab_agent.domain.enums import TxnState

_T0 = datetime.datetime(2026, 5, 31, 9, 0, tzinfo=datetime.UTC)
_T1 = datetime.datetime(2026, 5, 31, 9, 5, tzinfo=datetime.UTC)


def test_empty_log_starts_at_seq_zero() -> None:
    assert AuditLog().next_seq == 0
    assert AuditLog().entries == ()


def test_append_assigns_monotonic_seq() -> None:
    log = (
        AuditLog()
        .append(
            StateChanged(to_state=TxnState.ENRICHING, trigger="snapshot"),
            at=_T0,
        )
        .append(MessageSent(action_seq=1, purpose="ask"), at=_T1)
    )
    assert [e.seq for e in log.entries] == [0, 1]
    assert log.next_seq == 2
    assert log.entries[1].at == _T1


def test_append_is_pure_and_does_not_mutate_the_original() -> None:
    original = AuditLog()
    grown = original.append(MessageSent(action_seq=1, purpose="fyi"), at=_T0)
    assert original.entries == ()
    assert len(grown.entries) == 1
    assert grown.entries[0].event == MessageSent(action_seq=1, purpose="fyi")
