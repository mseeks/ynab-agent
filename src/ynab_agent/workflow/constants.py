"""Shared operational constants for the transaction workflows.

Kept import-light (stdlib + the Temporal SDK) so the workflow sandboxes can pass
it through without pulling domain types. The poller (W1) sets its own longer
window for delta fetches; these are the short request/response workflows'
defaults.

These knobs encode the system's whole failure philosophy: retry a *transient*
blip until it heals, fail a *deterministic* bug immediately, and — the part that
earns this module its long comment — bound the retrying by the **wall clock**,
not the attempt count, so a stuck activity always surfaces (and alerts) within a
predictable window.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

# Default per-attempt timeout for the short workflows (W2/W3/W4). One window
# fits them all, sized very generously for the slowest: the agentic activities
# call a local Gemma 4 31b over the tailnet *with reasoning on*, where a cold
# 19 GB model load plus a long reasoned generation can run for several minutes.
# We deliberately favour completion over speed (intelligence is the point), so
# the ceiling sits well above the model's request timeout (see `agentic.model`,
# 1200 s), which trips first and is retried. The fast I/O activities
# (YNAB/AgentMail) finish in well under a second, so the high ceiling only
# bounds a genuine hang. This is ``start_to_close`` — the budget below bounds
# the whole retrying lifecycle.
ACTIVITY_TIMEOUT = timedelta(seconds=1800)

# ── The retry budget: bound the wall clock, not the attempt count ─────────────
#
# Why not ``maximum_attempts``? Because per-attempt cost in this system spans
# ~1000x, so any single attempt count yields wildly different real-time windows:
#
#   * a fast-fail (YNAB down, Ollama *unreachable* -> connection refused) fails
#     in well under a second, so the backoff dominates;
#   * a *hung* model attempt runs to the 1200 s (20 min) request timeout in
#     `agentic.model` before it fails.
#
# So ``maximum_attempts=30`` means ~25 min for the fast-fail case but up to ~10
# *hours* for a hung model — and that 1000x spread exists even *within* the
# single `enrich` activity ("Ollama asleep" fails in ms; "Ollama wedged" takes
# 20 min). No attempt count can mean "give up after ~X minutes" across that.
#
# ``schedule_to_close_timeout`` (applied at each ``execute_activity`` call, set
# to this budget) bounds the activity's *entire* retrying life — all attempts
# plus backoff — by elapsed time. It auto-adapts to each activity's per-attempt
# cost: a deterministic bug fails at attempt 1 (the non-retryable list below)
# and alerts in seconds regardless of the budget; anything transient retries
# until the budget, then goes terminal and alerts. Same predictable window
# whether the failure is fast-fail spam or a slow hang.
#
# Why 45 minutes? It's "how long an outage lasts before you get paged." 45 min
# rides out the common brief outages (a Mac Studio reboot, an Ollama restart, a
# transient YNAB/AgentMail blip) without crying wolf, yet surfaces a *real*
# outage well before the hourly poll re-fire. And it must exceed
# ``ACTIVITY_TIMEOUT`` (one legitimate cold-load generation) so a slow attempt
# is never guillotined as if it were a failure — 45 min > 30 min, with room for
# a retry. The W1 poll loop re-addresses a failed W2 every tick (hourly, via
# ``ALLOW_DUPLICATE_FAILED_ONLY``), so this budget need not survive a multi-hour
# outage — the poll is the long-horizon retry; this is the short one. Net: one
# alert ~45 min into a real outage, silence for blips shorter than that, instant
# alert for actual bugs.
ACTIVITY_BUDGET = timedelta(minutes=45)

# Cap the exponential backoff so fast-fail retries settle to a steady cadence
# (~once every 2 min) instead of ballooning toward the budget on their own. With
# this cap a fast-fail failure gets ~25 retries inside the 45 min budget —
# plenty to ride out a blip — rather than a handful of ever-longer sleeps.
_MAX_RETRY_INTERVAL = timedelta(seconds=120)

# Deterministic failures a retry cannot fix: a malformed model output, a bad
# payload, a programming error. Temporal records the raised exception's type
# name, so we match by name — no need to import the activity-layer types into
# the sandbox. This is a denylist (Temporal retries by default), so it must be
# kept reasonably complete for the common bug classes — but ``ACTIVITY_BUDGET``
# above is the real backstop for any type we forget: an unenumerated exception
# still goes terminal when the budget elapses, instead of spinning forever.
# (``AttributeError`` was the one missing here that let a registry-deserial-
# ization bug retry 500+ times in production — #4.)
_NON_RETRYABLE = (
    "ValueError",
    "TypeError",
    "KeyError",
    "AttributeError",
    "IndexError",
    "NameError",
    "NotImplementedError",  # the W4/W6 activity stubs raise this
    "AssertionError",
    "ValidationError",  # pydantic
    "UnexpectedModelBehavior",  # pydantic-ai
)

# The retry policy every activity call uses (SPEC §0.5). Attempts stay unbounded
# — ``ACTIVITY_BUDGET`` (the per-call ``schedule_to_close_timeout``) is the
# bound, not a count — so the durable self-heal survives a transient YNAB /
# AgentMail / model blip without dropping a transaction, while the non-retryable
# list fails a deterministic bug fast and the budget stops everything else from
# spinning past its window.
ACTIVITY_RETRY = RetryPolicy(
    non_retryable_error_types=list(_NON_RETRYABLE),
    maximum_interval=_MAX_RETRY_INTERVAL,
)

# ── The alerting path: fast and best-effort ───────────────────────────────────
# The failure-alert activities (the ntfy push and the dedup-ledger reads) run
# *while a transaction is already failing*, so they must be quick and must never
# become the thing that spins. Short ceiling, short total budget, few attempts —
# a missed alert is acceptable (the send itself swallows its own errors); a
# slow or looping alert path is not.
ALERT_TIMEOUT = timedelta(seconds=30)
ALERT_BUDGET = timedelta(seconds=90)
ALERT_RETRY = RetryPolicy(
    maximum_attempts=3,
    maximum_interval=timedelta(seconds=10),
)
