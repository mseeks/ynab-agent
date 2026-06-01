"""Tests for the W1 poll-loop starter's pure config logic (SPEC §2)."""

from __future__ import annotations

import datetime

import pytest

from ynab_agent.poll_starter import _resolve_install_date

_TODAY = datetime.date(2026, 5, 31)


def test_install_date_uses_configured_iso_date() -> None:
    assert _resolve_install_date("2026-01-15", today=_TODAY) == datetime.date(
        2026, 1, 15
    )


def test_install_date_defaults_to_about_ninety_days_back() -> None:
    assert _resolve_install_date(None, today=_TODAY) == datetime.date(
        2026, 3, 2
    )


def test_install_date_empty_string_falls_back_to_default() -> None:
    assert _resolve_install_date("", today=_TODAY) == datetime.date(2026, 3, 2)


def test_install_date_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat"):
        _resolve_install_date("not-a-date", today=_TODAY)
