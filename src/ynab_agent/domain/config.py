"""Lifecycle policy: the timer windows the state machine reads (SPEC §11).

The state machine is pure but must set absolute deadlines (the SPEC mandates
absolute timestamps, not durations). It computes them as ``now + window`` from
this config, so the windows are tunable in one place. Heavier policy (the
autonomy gate, the hard floor) lives elsewhere; this holds only the timers.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from ynab_agent.domain.base import Frozen


class LifecyclePolicy(Frozen):
    """Tunable timer windows (defaults from SPEC §11)."""

    patience_window: timedelta = Field(default=timedelta(days=7))
    amazon_hold: timedelta = Field(default=timedelta(hours=36))
    archive_window: timedelta = Field(default=timedelta(days=30))


DEFAULT_POLICY = LifecyclePolicy()
