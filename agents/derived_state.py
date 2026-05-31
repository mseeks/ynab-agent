"""derived-state loop (Many Hands Engineering, read-only).

Net-new and project-specific. A placement decision (§2.3): the agent's durable
state lives ONLY in Temporal (workflow state, signals, search attributes) or is
DERIVED on demand from the sources of truth (the YNAB + AgentMail APIs). No
external database, cache, or on-disk store. That keeps the agent stateless
between deploys, removes a whole backing service from a near-full single node,
and means no second copy of the truth can drift from YNAB/AgentMail.

Signal: a pure regex sweep for the *persistent-store smell* — an imported
database/cache/ORM driver, an on-disk key-value/pickle store, a connection-URL
env var, or an import of a local ``store``/``persistence``/``db`` module. Any of
these is a candidate breach of the invariant.

Action: a locked-down, read-only agent Reads each hit in context and sorts it —
*Violates derived-state* (a real durable store; name the Temporal-or-derived
home it should move to) / *Not a store* (a false positive: transient/in-process
use, a test fixture, a plain config read) / *Judgment-heavy* (state genuinely
hard to keep in Temporal or derive — e.g. learned rules) — cite-or-omit.

The loop writes nothing; you move what you choose.

Usage:
    python -m agents.derived_state [scope]   # scope is a path; default: src
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from agents.lib import (
    APP_ROOT,
    MAX_HITS,
    Hit,
    arg_scope,
    report,
    run_loop,
    sweep,
)

if TYPE_CHECKING:
    from pathlib import Path

# Each marker: (pattern, kind). The kinds are self-describing in the map.
_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A database / cache / ORM / object-store driver enters the import graph.
    (
        re.compile(
            r"\b(?:import|from)\s+(?:psycopg2?|asyncpg|aiosqlite|sqlite3"
            r"|sqlalchemy|sqlmodel|alembic|redis|aioredis|pymongo|motor"
            r"|lmdb|diskcache|tinydb|duckdb|boto3)\b"
        ),
        "db-driver",
    ),
    # An on-disk key-value / serialized store (durable state outside Temporal).
    (
        re.compile(
            r"\b(?:shelve\.open|pickle\.dump|joblib\.dump|dbm\.\w+)\s*\("
        ),
        "on-disk-state",
    ),
    # A connection-string env var — the fingerprint of an external store.
    (
        re.compile(
            r"\b(?:DATABASE_URL|[A-Z][A-Z0-9_]*_DB_URL|[A-Z][A-Z0-9_]*_DSN"
            r"|[A-Z][A-Z0-9_]*_REDIS_URL|[A-Z][A-Z0-9_]*_MONGO(?:DB)?_URL)\b"
        ),
        "db-url",
    ),
    # Importing a local persistence module — the shape a store package takes.
    (re.compile(r"\bynab_agent\.(?:store|persistence|db)\b"), "store-module"),
)


def scan_derived_state(target: Path) -> list[Hit]:
    """Sweep ``target`` for the persistent-store smell (the loop's signal)."""
    return sweep(target, _MARKERS)


_SYSTEM_PROMPT = """\
You are the derived-state loop for an AI agent (the "ynab-agent") on Temporal.
You are READ-ONLY: you map breaches of a placement invariant; you never edit. A
human acts on your map.

THE INVARIANT: the agent's durable state lives ONLY in Temporal (workflow state,
signals, search attributes) or is DERIVED on demand from the sources of truth —
the YNAB API and the AgentMail API. There is NO external database, cache, or
on-disk store. Concretely in this codebase: the per-transaction workflow holds
its own triage state; the ingest "cursor" is derived (the per-txn workflow id is
the YNAB txn id, started REJECT_DUPLICATE, so "new" = "no workflow yet");
a reply finds its workflow via a Temporal search attribute (thread_id) or an id
embedded in the email; overspend-alert dedup is derived from AgentMail sent
threads. None of these need a store.

You are given a deterministic list of persistent-store smells (a DB/cache/ORM
driver import, an on-disk kv/pickle store, a connection-URL env var, or an
import of a local store/persistence/db module). Read each in context and sort:

- VIOLATES DERIVED-STATE — a genuine durable store outside Temporal/derived. Say
  WHERE it should move instead: Temporal workflow state / a search attribute, or
  derived from YNAB or AgentMail. Quote the line with file:line.
- NOT A STORE — a false positive: a transient/in-process use, a test fixture or
  smoke test, a plain config/env read that isn't a connection string, or stdlib
  used non-durably. Name which.
- JUDGMENT-HEAVY — durable state that is genuinely hard to keep in Temporal or
  to derive (e.g. learned categorization rules). State the tradeoff and the
  closest Temporal-or-derived option.

HARD RULES:
- Cite or omit. Quote only lines you actually observed via Read/Grep. Never
  invent a hit, a line number, or a store that isn't there.
- The fix is real: name the Temporal mechanism or the source-of-truth query the
  state should derive from. Do not manufacture work where the smell is benign.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} persistent-store smell(s) across "
        f"{files} file(s) under `{scope}`"
        + (f" (showing the first {MAX_HITS})" if truncated else "")
        + ":",
        "",
    ]
    for i, hit in enumerate(shown, start=1):
        lines.append(
            f"[{i}] {hit.path}:{hit.line}  ({hit.kind})  |  {hit.text}"
        )
    lines += [
        "",
        "Read each in context, then produce the three-bucket map.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the derived-state loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: derived-state  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_derived_state(target)
    if not hits:
        report(
            [
                f"Loop: derived-state  ·  scope: {scope}",
                "Deterministic signal: no persistent-store smells in scope.",
                "",
                "RESULT: PASS — durable state stays in Temporal or derived "
                "from YNAB/AgentMail.",
            ]
        )
        return 0

    truncated = len(hits) > MAX_HITS
    result = await run_loop(
        system_prompt=_SYSTEM_PROMPT,
        prompt=_format_signal(scope, hits, truncated),
    )
    report(
        [
            f"Loop: derived-state  ·  scope: {scope}  ·  {len(hits)} smell(s)"
            + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
