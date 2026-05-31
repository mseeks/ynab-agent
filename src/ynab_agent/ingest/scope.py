"""The fail-closed ingestion scope (SPEC §0.6, §13).

v1 targets exactly one named YNAB budget, optionally a subset of accounts, and
ignores anything dated before install. The scope is checked *before* any W2 is
addressed, so an out-of-scope transaction never enters the system.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import AccountId

if TYPE_CHECKING:
    from ynab_agent.domain.transaction import YnabSnapshot


class IngestScope(Frozen):
    """The budget, account subset, and install cutover that bound ingestion.

    Attributes:
        budget_id: The single YNAB budget v1 operates on.
        install_date: Transactions dated before this are ignored (cold-start
            cutover, SPEC §13).
        account_ids: An optional account allow-list; ``None`` means all accounts
            in the budget.
    """

    budget_id: str
    install_date: datetime.date
    account_ids: frozenset[AccountId] | None = None


def in_scope(snapshot: YnabSnapshot, scope: IngestScope) -> bool:
    """Whether a transaction falls within the fail-closed ingestion scope."""
    if snapshot.txn_date < scope.install_date:
        return False
    return scope.account_ids is None or snapshot.account in scope.account_ids
