"""YNAB reader: the budget surface the agent acts on (best-effort).

Reuses the production :class:`~ynab_agent.ynab.client.YnabClient` (its tested
wire-parsing and budget-id resolution) off the event loop via
``asyncio.to_thread`` — no parallel REST surface, and the client is never
mutated. Surfaces the ``type=unapproved`` backlog (the W1 work queue) and the
overspent categories. Degrades to "off" when ``YNAB_API_KEY`` is unset.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from ynab_agent.dashboard.model import Budget, CategoryRow, TxnFacts, TxnRow

if TYPE_CHECKING:
    from ynab_agent.domain.transaction import YnabSnapshot

_MAX_SAMPLE = 8
_MAX_OVERSPENT = 8

# Cap on how many awaiting ids one render will resolve against YNAB (the queue
# is already bounded upstream; this is a runaway backstop).
_MAX_RESOLVE = 40

# Wall-clock bound on the resolve so a slow YNAB never hangs a page render (the
# worker thread may finish in the background; the page proceeds with bare ids).
_RESOLVE_TIMEOUT = 12.0

# A W2 id is the bare YNAB txn id, but a few carry a ``_YYYY-MM-DD`` suffix
# (scheduled/split origins); strip it for the YNAB lookup, keep the raw id key.
_DATE_SUFFIX = re.compile(r"_\d{4}-\d{2}-\d{2}$")


def _read() -> Budget:
    """Read the budget surface synchronously (run in a worker thread)."""
    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    unapproved = client.unapproved()
    spends = client.category_spends()

    sample = tuple(
        TxnRow(payee=snap.payee, amount=str(snap.amount))
        for snap in unapproved[:_MAX_SAMPLE]
    )
    overspent_spends = sorted(
        (s for s in spends if s.balance.milliunits < 0),
        key=lambda s: s.balance.milliunits,
    )
    overspent = tuple(
        CategoryRow(name=s.name, balance=str(s.balance), overspent=True)
        for s in overspent_spends[:_MAX_OVERSPENT]
    )
    return Budget(
        available=True,
        unapproved=len(unapproved),
        unapproved_sample=sample,
        overspent=overspent,
    )


async def fetch() -> tuple[Budget, str | None]:
    """Read the budget surface; ``error`` is "off" when YNAB is unconfigured."""
    try:
        budget = await asyncio.to_thread(_read)
    except RuntimeError:
        return Budget(available=False), "off"
    except Exception as exc:  # any YNAB hiccup degrades to a red dot
        return Budget(available=False), f"{type(exc).__name__}: {exc}"
    return budget, None


def _facts(snap: YnabSnapshot, categories: dict[str, str]) -> TxnFacts:
    """Reduce a snapshot to the humanizing facts the queue shows."""
    category = (
        categories.get(str(snap.category_id))
        if snap.category_id is not None
        else None
    )
    return TxnFacts(
        payee=snap.payee or "(no payee)",
        amount=str(snap.amount),
        approved=snap.approved,
        category=category,
    )


def _resolve(ids: tuple[str, ...]) -> dict[str, TxnFacts]:
    """Resolve queued workflow ids to YNAB facts (run in a worker thread).

    The still-*unapproved* ids (the ones genuinely waiting on the owner) come
    from one ``unapproved`` index — no per-id list scan. The rest are the
    proposals the owner already approved in-app: a cheap single GET each
    (``snapshot`` returns those without a scan). Keyed by the raw id so the
    read-model can join on the workflow id verbatim.
    """
    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    categories = {str(s.category): s.name for s in client.category_spends()}
    unapproved = {str(s.ynab_id): s for s in client.unapproved()}

    out: dict[str, TxnFacts] = {}
    for raw in ids:
        bare = _DATE_SUFFIX.sub("", raw)
        snap = unapproved.get(bare) or client.snapshot(bare)
        if snap is not None:
            out[raw] = _facts(snap, categories)
    return out


async def resolve_queue(ids: tuple[str, ...]) -> dict[str, TxnFacts]:
    """Humanize the awaiting-queue ids; ``{}`` whenever YNAB is unreachable.

    Best-effort by design: any failure (or YNAB being off) yields an empty map
    and the renderer degrades each row to its short id — the queue never fails
    the page.
    """
    unique = tuple(dict.fromkeys(i for i in ids if i))[:_MAX_RESOLVE]
    if not unique:
        return {}
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_resolve, unique), _RESOLVE_TIMEOUT
        )
    except Exception:  # timeout / YNAB off / any hiccup → bare-id fallback
        return {}
