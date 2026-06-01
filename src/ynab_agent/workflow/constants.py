"""Shared operational constants for the transaction workflows.

Kept import-light (stdlib only) so the workflow sandboxes can pass it through
without pulling domain types. The poller (W1) sets its own longer window for
delta fetches; these are the short request/response workflows' defaults.
"""

from __future__ import annotations

from datetime import timedelta

# Default per-activity timeout for the short workflows (W2/W3/W4). One window
# fits them all, sized very generously for the slowest: the agentic activities
# call a local Gemma over the tailnet, where a cold model load plus a long
# generation can run for minutes. The fast I/O activities (YNAB/AgentMail)
# finish in well under a second, so the high ceiling only bounds a genuine hang.
ACTIVITY_TIMEOUT = timedelta(seconds=900)
