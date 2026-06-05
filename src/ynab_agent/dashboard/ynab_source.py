"""YNAB reader: the budget surface the agent acts on (best-effort).

Reuses the production :class:`~ynab_agent.ynab.client.YnabClient` (its tested
wire-parsing and budget-id resolution) off the event loop via
``asyncio.to_thread`` — no parallel REST surface, and the client is never
mutated. Surfaces the ``type=unapproved`` backlog (the W1 work queue) and the
overspent categories. Degrades to "off" when ``YNAB_API_KEY`` is unset.
"""

from __future__ import annotations

import asyncio

from ynab_agent.dashboard.model import Budget, CategoryRow, TxnRow

_MAX_SAMPLE = 8
_MAX_OVERSPENT = 8


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
