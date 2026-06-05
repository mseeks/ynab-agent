"""The alert ledger's pure state and folds — the anti-bombard policy (SPEC §13).

Two rules turn "alert immediately on every failure" into "alert promptly but
never flood":

* **Per-key cooldown** (default 24 h): a given failure key — a transaction id —
  alerts at most once per cooldown. The W1 poll re-fires a failed W2 every tick
  (hourly), so without this a single deterministically-broken transaction would
  ping every hour forever; with it, once a day.
* **Global rate cap** (default 5 / hour): independent of the per-key rule, no
  more than N alerts fire in any rolling hour. A systemic break (a bad deploy
  failing *every* transaction — distinct keys, so the cooldown doesn't catch
  them) pings a handful of times, then goes quiet. After a few "X failed"
  pings it is obvious the whole thing is down; the (N+1)th adds no information.

The ledger is an append-only tail of ``(key, at)`` entries, pruned to the
cooldown horizon so continued alerting never grows the carried state without
bound (the same shape as the rule registry's audit tail).
"""

from __future__ import annotations

import datetime

from ynab_agent.domain.base import Frozen

# How long the same failure key stays quiet after an alert. A re-fired failure
# (the hourly poll re-addressing a still-broken transaction) is the same key, so
# this is what collapses "every poll tick" down to "once a day".
DEFAULT_COOLDOWN = datetime.timedelta(hours=24)

# The rolling window and ceiling for the global rate cap — the guard against a
# systemic break (many *distinct* keys failing at once) turning into a flood.
DEFAULT_RATE_WINDOW = datetime.timedelta(hours=1)
DEFAULT_RATE_CAP = 5


class AlertEntry(Frozen):
    """One alert that fired: its dedup key and when it went out."""

    key: str
    at: datetime.datetime


class LedgerState(Frozen):
    """The pruned tail of recent alerts — the whole dedup memory.

    Held as Temporal workflow state and carried across continue-as-new, so it is
    a frozen value like the rest of the domain.
    """

    entries: tuple[AlertEntry, ...] = ()


def should_notify(
    state: LedgerState,
    key: str,
    now: datetime.datetime,
    *,
    cooldown: datetime.timedelta = DEFAULT_COOLDOWN,
    rate_window: datetime.timedelta = DEFAULT_RATE_WINDOW,
    rate_cap: int = DEFAULT_RATE_CAP,
) -> bool:
    """Whether an alert for ``key`` should fire now (SPEC §13). Pure.

    ``False`` if this key alerted within ``cooldown`` (per-key dedup) or if the
    rolling-``rate_window`` alert count has reached ``rate_cap`` (global flood
    guard); ``True`` otherwise.
    """
    for entry in state.entries:
        if entry.key == key and now - entry.at < cooldown:
            return False
    recent = sum(1 for entry in state.entries if now - entry.at < rate_window)
    return recent < rate_cap


def record(
    state: LedgerState,
    key: str,
    now: datetime.datetime,
    *,
    retention: datetime.timedelta = DEFAULT_COOLDOWN,
) -> LedgerState:
    """Append ``key@now`` and prune entries older than ``retention``. Pure.

    The tail stays bounded — the cooldown is the longest horizon any rule looks
    back — so continued alerting never grows the carried state without limit.
    """
    kept = tuple(entry for entry in state.entries if now - entry.at < retention)
    return LedgerState(entries=(*kept, AlertEntry(key=key, at=now)))
