"""Shared operational constants for the transaction workflows.

Kept import-light (stdlib + the Temporal SDK) so the workflow sandboxes can pass
it through without pulling domain types. The poller (W1) sets its own longer
window for delta fetches; these are the short request/response workflows'
defaults.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

# Default per-activity timeout for the short workflows (W2/W3/W4). One window
# fits them all, sized very generously for the slowest: the agentic activities
# call a local Gemma 4 31b over the tailnet *with reasoning on*, where a cold
# 19 GB model load plus a long reasoned generation can run for several minutes.
# We deliberately favour completion over speed (intelligence is the point), so
# the ceiling sits well above the model's request timeout (see `agentic.model`),
# which trips first and is retried. The fast I/O activities (YNAB/AgentMail)
# finish in well under a second, so the high ceiling only bounds a genuine hang.
ACTIVITY_TIMEOUT = timedelta(seconds=1800)

# Deterministic failures a retry cannot fix: a malformed model output, a bad
# payload, a programming error. Temporal records the raised exception's type
# name, so we match by name — no need to import the activity-layer types into
# the sandbox.
_NON_RETRYABLE = (
    "ValueError",
    "TypeError",
    "KeyError",
    "AssertionError",
    "ValidationError",  # pydantic
    "UnexpectedModelBehavior",  # pydantic-ai
)

# The retry policy every activity call uses (SPEC §0.5). Unbounded attempts keep
# the durable self-heal so a transient YNAB / AgentMail / model blip never drops
# a transaction; the non-retryable list makes a deterministic failure fail fast
# and surface instead of spinning forever.
ACTIVITY_RETRY = RetryPolicy(non_retryable_error_types=list(_NON_RETRYABLE))
