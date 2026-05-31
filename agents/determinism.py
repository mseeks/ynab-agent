"""determinism loop (Many Hands Engineering, read-only).

Net-new for this project — Temporal-specific, with no Revisionist analogue. A
Temporal workflow must replay deterministically: every run of its history has to
produce the same commands. Reading the wall clock, rolling a random value,
minting a uuid, or touching the environment *in workflow code* breaks that — the
replay diverges from the original and the workflow can get wedged (this is the
exact class of bug behind an earlier multi-hour debugging session). The durable
spine must instead use ``workflow.now()`` / ``workflow.random()`` /
``workflow.uuid4()`` / ``workflow.sleep()``, or push the nondeterminism into an
*activity* (where anything goes).

Signal: a pure scan of *workflow files only* (those that contain
``@workflow.defn``) for the bare nondeterministic constructs. The patterns match
``datetime.now(`` / ``random.`` / ``uuid.`` / ``asyncio.sleep(`` etc. but
structurally cannot match the safe ``workflow.*`` forms, so those never flag.

Action: a locked-down, read-only agent Reads each hit and sorts it — *Hazard*
(genuinely in replay code; name the ``workflow.*`` replacement) / *Safe* (a
false positive: inside an activity, a comment, the imports block, or an
over-matched workflow.* form) / *Judgment-heavy* — quoting the line
(cite-or-omit). The loop writes nothing; you fix what it confirms.

Usage:
    python -m agents.determinism [scope]   # a path; default: src
"""

from __future__ import annotations

import asyncio
import re
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

# Only files that define a workflow carry the replay-determinism constraint.
_WORKFLOW_MARKER = "@workflow.defn"

# Bare nondeterministic constructs. Each is anchored so the safe ``workflow.*``
# form (workflow.now / workflow.random / workflow.uuid4 / workflow.sleep) cannot
# match: those have no ``datetime.``/``random.``/``uuid.``/``asyncio.`` prefix.
_HAZARDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdatetime\.(?:datetime\.)?now\("), "datetime.now"),
    (re.compile(r"\b(?:datetime\.)?utcnow\("), "utcnow"),
    (re.compile(r"\bdate\.today\("), "date.today"),
    (re.compile(r"\btime\.(?:time|monotonic|perf_counter)\("), "time.clock"),
    (re.compile(r"\brandom\.\w"), "random module"),
    (re.compile(r"\buuid\.\w"), "uuid module"),
    (re.compile(r"\bos\.(?:environ|getenv)\b"), "os env"),
    (re.compile(r"\basyncio\.sleep\("), "asyncio.sleep"),
)


def scan_determinism_hazards(target: Path) -> list[Hit]:
    """Find bare nondeterministic constructs in workflow files (the signal).

    A file is only scanned if it defines a workflow (``@workflow.defn``);
    nondeterminism in plain activity modules is allowed and never flagged.
    """
    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        if _WORKFLOW_MARKER not in text:
            continue
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            snippet = raw.strip()[:SNIPPET_MAX]
            for pattern, kind in _HAZARDS:
                if pattern.search(raw):
                    hits.append(Hit(rel, lineno, kind, snippet))
    return sorted(hits, key=lambda h: (h.path, h.line, h.kind))


_SYSTEM_PROMPT = """\
You are the determinism loop for a Temporal + Pydantic project. You are
READ-ONLY: you map replay-nondeterminism hazards in workflow code; you never
edit. A human fixes what you confirm.

A Temporal workflow must replay deterministically. In WORKFLOW code (a
`@workflow.defn` class and what it calls in-process) the wall clock, randomness,
uuids, the environment, and raw sleeps are forbidden — they must use
`workflow.now()` / `workflow.random()` / `workflow.uuid4()` / `workflow.sleep`,
or move into an ACTIVITY, where nondeterminism is fine.

You are given a deterministic list of bare nondeterministic constructs found in
files that define a workflow. Read each in context and sort it into one bucket:

- HAZARD — genuinely reached during replay (in the workflow class or a helper it
  calls in-process). Name the exact `workflow.*` replacement or say "move to an
  activity". Quote the line.
- SAFE — a false positive: the line is inside an ACTIVITY defined in the same
  file, in a comment/docstring, in the `imports_passed_through()` block, or a
  type annotation (e.g. `datetime.datetime` as a type, not a `.now()` call).
  Name which.
- JUDGMENT-HEAVY — technically nondeterministic but plausibly benign (e.g. it
  never influences workflow state or a command). State the tradeoff.

HARD RULES:
- Cite or omit. Quote the line you Read, or drop the hit. Never invent.
- The safe replacements are real: prefer naming the specific `workflow.*` call.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} hit(s) across {files} workflow "
        f"file(s) under `{scope}`"
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
    """Run the determinism loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: determinism  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_determinism_hazards(target)
    if not hits:
        report(
            [
                f"Loop: determinism  ·  scope: {scope}",
                "Deterministic signal: no bare nondeterminism in workflows.",
                "",
                "RESULT: PASS — workflow code looks replay-safe in scope.",
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
            f"Loop: determinism  ·  scope: {scope}  ·  "
            f"{len(hits)} hit(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
