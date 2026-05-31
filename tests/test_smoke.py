"""Smoke test: the package imports and the toolchain is wired correctly."""

from __future__ import annotations

import ynab_agent


def test_version_is_exposed() -> None:
    assert ynab_agent.__version__ == "0.1.0"
