"""Household config the pure core reads: timer windows + the timezone (§11).

The state machine is pure but must set absolute deadlines (the SPEC mandates
absolute timestamps, not durations). It computes them as ``now + window`` from
this config, so the windows are tunable in one place. Heavier policy (the
autonomy gate, the hard floor) lives elsewhere; this holds the timers and the
one declared household timezone.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from pydantic import Field

from ynab_agent.domain.base import Frozen


class LifecyclePolicy(Frozen):
    """Tunable timer windows (defaults from SPEC §11)."""

    patience_window: timedelta = Field(default=timedelta(days=7))
    amazon_hold: timedelta = Field(default=timedelta(hours=36))
    archive_window: timedelta = Field(default=timedelta(days=30))


DEFAULT_POLICY = LifecyclePolicy()

# The one declared household timezone (SPEC §11, §13). Every day/month
# boundary the agent reasons about — the W6 budget month and run-rate, the
# dashboard's "as of" stamp (and, as they land, the Amazon 02:00 expectation and
# receipt date-proximity) — is derived in this zone, not UTC, so a charge near
# midnight or a month boundary lands in the right day/month. ``workflow.now()``
# stays the deterministic clock; it is only *converted* to this zone for
# bucketing and display, never used as a non-deterministic local clock.
HOUSEHOLD_TZ = ZoneInfo("America/Chicago")
