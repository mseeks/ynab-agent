"""Shared operational constants for the transaction workflows.

Kept import-light (stdlib only) so the workflow sandboxes can pass it through
without pulling domain types. The poller (W1) sets its own longer window for
delta fetches; these are the short request/response workflows' defaults.
"""

from __future__ import annotations

from datetime import timedelta

# Default per-activity timeout for the short workflows (W2/W3/W4). All of their
# activities are quick request/response calls, so one window fits them.
ACTIVITY_TIMEOUT = timedelta(seconds=30)
