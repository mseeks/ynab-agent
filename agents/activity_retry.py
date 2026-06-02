"""activity-retry loop (Many Hands Engineering, read-only).

Net-new and Temporal-specific. When ``workflow.execute_activity`` is called with
no ``retry_policy``, the activity inherits Temporal's default policy —
*unlimited* attempts with exponential backoff. For a transient I/O blip that is
the right, self-healing behaviour. For a **deterministic** failure it is a trap:
a schema/validation/4xx error (the class behind the Ollama #15288 outage) can
never succeed, so the activity retries forever, burning the full
``start_to_close_timeout`` on every doomed attempt — a silent, expensive spin
rather than a clean failure that surfaces.

The discipline is to set an explicit ``retry_policy`` on each call — a bounded
``maximum_attempts`` and/or ``non_retryable_error_types`` for the
deterministic-failure-prone activities (the agentic steps and the writes),
keeping the generous default only where unbounded retry is genuinely correct.

Signal: a pure AST scan for ``workflow.execute_activity`` /
``execute_local_activity`` calls whose keyword arguments do **not** include
``retry_policy`` (an AST walk handles the multi-line calls a line regex would
miss). It flags the call site; the agent judges whether unbounded retry is a
hazard there.

Action: a read-only agent Reads each call and sorts it — *No policy*
(a write or agentic activity that must bound retries / mark non-retryable;
quote the line) / *Acceptable* (an idempotent, retry-safe read where the default
is fine, or the policy is supplied via a shared default) / *Judgment-heavy*
(tune ``maximum_attempts`` vs ``non_retryable_error_types``) — cite-or-omit.

Usage:
    python -m agents.activity_retry [scope]   # a path; default: src
"""

from __future__ import annotations

import ast
import asyncio
from typing import TYPE_CHECKING

from agents.lib import (
    APP_ROOT,
    MAX_HITS,
    SNIPPET_MAX,
    Hit,
    arg_scope,
    iter_python_files,
    report,
    run_loop,
)

if TYPE_CHECKING:
    from pathlib import Path

# The activity-scheduling calls a workflow makes; both take a ``retry_policy``.
_SCHEDULERS = ("execute_activity", "execute_local_activity")
_POLICY_KW = "retry_policy"


def _has_retry_policy(call: ast.Call) -> bool:
    """Whether a call passes a ``retry_policy`` keyword (incl. a ``**`` spread).

    A ``**kwargs`` spread (a keyword with ``arg is None``) could carry the
    policy, so it counts as present — the agent then checks the source.
    """
    return any(kw.arg == _POLICY_KW or kw.arg is None for kw in call.keywords)


def _is_scheduler(call: ast.Call) -> str | None:
    """Return the scheduler method name a call invokes, or None.

    Matches on the attribute name (``.execute_activity``) so an aliased or
    re-exported ``workflow`` still resolves.
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _SCHEDULERS:
        return func.attr
    return None


def scan_activity_retry(target: Path) -> list[Hit]:
    """Find activity-scheduling calls lacking a ``retry_policy`` (the signal).

    Args:
        target: A file or directory to scan.

    Returns:
        Hits ordered by path then line.
    """
    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            method = _is_scheduler(node)
            if method is None or _has_retry_policy(node):
                continue
            snippet = lines[node.lineno - 1].strip()[:SNIPPET_MAX]
            hits.append(Hit(rel, node.lineno, method, snippet))
    return sorted(hits, key=lambda h: (h.path, h.line))


_SYSTEM_PROMPT = """\
You are the activity-retry loop for a Temporal project. You are READ-ONLY: you
map activity calls with no explicit retry policy; you never edit. A human fixes
what you confirm.

A `workflow.execute_activity(...)` call with no `retry_policy` inherits
Temporal's default: UNLIMITED attempts with exponential backoff. That is correct
for a transient I/O failure (it self-heals), but a trap for a DETERMINISTIC
failure — a schema/validation/4xx error never succeeds, so it spins forever,
burning the full `start_to_close_timeout` each attempt. The fix is an explicit
`retry_policy` with a bounded `maximum_attempts` and/or
`non_retryable_error_types`.

You are given a list of activity calls that pass no `retry_policy`.
Read each in context (what activity does it schedule? what can it raise?) and
sort it into exactly one bucket:

- NO POLICY — a write (commit/send) or an agentic/model activity that can fail
  deterministically and so should bound retries or mark errors non-retryable.
  Name the activity and quote the line.
- ACCEPTABLE — an idempotent, retry-safe READ where unbounded retry on a
  transient failure is genuinely the right default, OR the policy is supplied
  another way (a shared default, a `**kwargs` spread, a worker-level policy).
  Name why.
- JUDGMENT-HEAVY — needs a call on `maximum_attempts` vs
  `non_retryable_error_types` vs a `schedule_to_close` ceiling. State the
  tradeoff.

HARD RULES:
- Cite or omit. Quote the line you Read, or drop the hit. Never invent.
- Read the activity being scheduled before judging; "looks risky" is not enough.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} activity call(s) with no "
        f"retry_policy across {files} file(s) under `{scope}`"
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
    """Run the activity-retry loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: activity-retry  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_activity_retry(target)
    if not hits:
        report(
            [
                f"Loop: activity-retry  ·  scope: {scope}",
                "Deterministic signal: all activity calls set a retry_policy.",
                "",
                "RESULT: PASS — no activity inherits the unbounded default.",
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
            f"Loop: activity-retry  ·  scope: {scope}  ·  "
            f"{len(hits)} call(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
