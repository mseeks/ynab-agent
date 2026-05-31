"""W1 ingestion: the pure planning core for the YNAB delta poller (SPEC §2).

Decides which polled transactions to address, honoring the fail-closed scope,
the cold-start cutover, and the import lifecycle. The Temporal poll workflow
(``ynab_agent.workflow.poll_workflow``) executes the plan.
"""

from __future__ import annotations

from ynab_agent.ingest.plan import (
    AddressTxn,
    is_duplicate_import,
    plan_ingest,
)
from ynab_agent.ingest.scope import IngestScope, in_scope

__all__ = [
    "AddressTxn",
    "IngestScope",
    "in_scope",
    "is_duplicate_import",
    "plan_ingest",
]
