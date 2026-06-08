"""Shared operational constants for the transaction workflows.

Kept import-light (stdlib + the Temporal SDK) so the workflow sandboxes can pass
it through without pulling domain types. The poller (W1) sets its own longer
window for delta fetches; these are the short request/response workflows'
defaults.

These knobs encode the failure philosophy: retry a *transient* blip (bounded by
an attempt count), fail a *deterministic* bug immediately (the denylist), and
bound each attempt's *running* time (``start_to_close``). What they deliberately
do **not** set is a total wall-clock budget (``schedule_to_close``) — see the
``ACTIVITY_RETRY`` comment for why that is actively wrong on this worker.
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
# bounds a genuine hang. This is ``start_to_close`` — it counts only once an
# activity *starts*, so it never penalises time spent waiting in the queue.
ACTIVITY_TIMEOUT = timedelta(seconds=1800)

# Cap the exponential backoff so fast-fail retries settle to a steady cadence
# (~once every 2 min) rather than ballooning toward ever-longer sleeps.
_MAX_RETRY_INTERVAL = timedelta(seconds=120)

# Deterministic failures a retry cannot fix: a bad payload or a programming
# error. Temporal records the raised exception's type name, so we match by name
# — no need to import the activity-layer types into the sandbox. A denylist
# (Temporal retries by default), kept reasonably complete for the common bug
# classes; ``maximum_attempts`` below is the backstop for any type we forget.
# (``AttributeError`` was the one missing here that let a registry-
# deserialization bug retry 500+ times in production — #4.)
#
# Deliberately NOT listed (so it *is* retried): ``UnexpectedModelBehavior``. A
# local Gemma occasionally leaks a chat-template token or malforms its JSON,
# which pydantic-ai surfaces as this after its own output retries. The glitch is
# *transient*, not deterministic: a fresh generation almost always parses (8/8
# clean when reproduced). Treating it as terminal let one flaky reply-read kill
# a whole balance offer (the Transportation overspend page). Retrying re-runs
# the activity (a fresh model call); ``maximum_attempts`` still bounds the rare
# case where the model is genuinely, repeatably stuck.
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
)

# ── Bound the retrying by attempts, NOT a wall-clock budget ───────────────────
#
# An earlier cut bounded the whole retrying lifecycle with a 45-min
# ``schedule_to_close_timeout``. That is wrong for *this* worker, and it shipped
# a real regression (a SCHEDULE_TO_START false-alarm page — #6 follow-up):
#
#   * The worker is serial (one activity slot) and an agentic activity can hold
#     it for up to 1200 s. So a sub-second activity (e.g. ``open_thread``) can
#     sit in the task queue a long time behind an unrelated slow one — that
#     queue-wait is healthy backlog, not failure.
#   * ``schedule_to_close`` is measured from first-schedule, so it *counts that
#     queue-wait*. Worse, with no explicit ``schedule_to_start_timeout`` the
#     Temporal server normalises ``schedule_to_start := schedule_to_close``,
#     arming a real 45-min queue-wait guillotine. A fast activity that waits its
#     turn past 45 min dies never having run → ``SCHEDULE_TO_START`` → a page
#     for a worker that was merely busy.
#
# So we bound the two things that *are* failures — per-attempt run time
# (``start_to_close``, above) and the number of attempts — and leave queue-wait
# unbounded (a queued activity waits for the worker, then runs, as before #5).
# ``maximum_attempts`` rides out a transient blip and then gives up: a fast-fail
# (Ollama/YNAB unreachable, sub-second attempts) goes terminal in ~15 min with
# the 120 s backoff cap — long enough to ride a Mac reboot, short enough to
# surface a real outage before the hourly W1 poll re-fires. Deterministic bugs
# still fail at attempt 1 via the denylist; the poll is the long-horizon retry.
# (The alert path keeps a small ``schedule_to_close`` — ``ALERT_BUDGET`` —
# because those activities are fast and best-effort.)
_MAX_ATTEMPTS = 10

ACTIVITY_RETRY = RetryPolicy(
    non_retryable_error_types=list(_NON_RETRYABLE),
    maximum_interval=_MAX_RETRY_INTERVAL,
    maximum_attempts=_MAX_ATTEMPTS,
)

# ── The alerting path: fast and best-effort ───────────────────────────────────
# The failure-alert activities (the ntfy push and the dedup-ledger reads) run
# *while a transaction is already failing*, so they must be quick and must never
# become the thing that spins. Short ceiling, short total budget, few attempts —
# a missed alert is acceptable (the send itself swallows its own errors); a slow
# or looping alert path is not. These activities are fast, so a small
# ``schedule_to_close`` here is safe — and it stops a wedged worker from hanging
# the failing workflow on its own alert.
ALERT_TIMEOUT = timedelta(seconds=30)
ALERT_BUDGET = timedelta(seconds=90)
ALERT_RETRY = RetryPolicy(
    maximum_attempts=3,
    maximum_interval=timedelta(seconds=10),
)
