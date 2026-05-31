"""debug-cruft loop (Many Hands Engineering, read-only).

Signal: a pure regex sweep for leftover debugging and disabled tests —
``print(`` / ``breakpoint()`` / ``pdb``/``set_trace``, and pytest
``skip``/``skipif``/``xfail`` markers. The highest-severity case is a *stale*
skip: a test disabled for a reason that has since been resolved, silently no
longer running.

Action: a locked-down, read-only agent Reads each hit in context and classifies
it into a strict three-bucket map — *Delete now* / *Intentional / justified* /
*Stale skip — re-enable now* — quoting the line it observed (cite-or-omit). The
loop writes nothing; you delete / keep / re-enable what you choose.

Mirrors the type-debt loop's shape (Revisionist loop #8), sharing the harness.

Usage:
    python -m agents.debug_cruft [scope]   # scope is a path; default: src
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

# The agents/ harness legitimately prints (report()), but it is excluded from
# the sweep, so its prints never appear here.
_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bbreakpoint\s*\("), "breakpoint"),
    (re.compile(r"\b(?:i?pdb)\.set_trace\b"), "set_trace"),
    (re.compile(r"^\s*import\s+i?pdb\b"), "import-pdb"),
    (re.compile(r"\bprint\s*\("), "print"),
    (re.compile(r"@pytest\.mark\.(?:skip|skipif|xfail)\b"), "pytest-skip"),
    (re.compile(r"\bpytest\.(?:skip|xfail)\s*\("), "pytest-skip-call"),
)


def scan_debug_cruft(target: Path) -> list[Hit]:
    """Sweep ``target`` for debug cruft and disabled tests (the signal)."""
    return sweep(target, _MARKERS)


_SYSTEM_PROMPT = """\
You are the debug-cruft loop for a Python project. You are READ-ONLY: you map
leftover debugging and disabled tests; you never edit. A human acts on your map.

You are given a deterministic list of marker hits. For each, Read the
surrounding code and classify it into exactly one bucket:

- DELETE NOW — real leftover debug (a stray print, a breakpoint, a pdb import
  or set_trace) that should not be in committed code. Quote the line with
  file:line.
- INTENTIONAL / JUSTIFIED — a verified legitimate use: a print in a genuine
  CLI/output sink, or a skip/xfail with a documented, still-valid reason.
  State the SPECIFIC reason (quote the justifying comment).
- STALE SKIP — RE-ENABLE NOW — a skip/xfail whose named reason has been
  resolved per the surrounding code, so the test should run again.

HARD RULES:
- Cite or omit. Quote only lines you actually observed via Read/Grep. Never
  invent a marker, a line number, or a justification that isn't there.
- A skip with no reason is not automatically stale; say so under
  JUDGMENT if you cannot tell. Do not re-enable a test you cannot verify.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} debug-cruft marker(s) under "
        f"`{scope}`"
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
    """Run the debug-cruft loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [f"Loop: debug-cruft  ·  scope: {scope}", f"Not found: {target}"]
        )
        return 1

    hits = scan_debug_cruft(target)
    if not hits:
        report(
            [
                f"Loop: debug-cruft  ·  scope: {scope}",
                "Deterministic signal: 0 debug-cruft markers found.",
                "",
                "RESULT: PASS — no print / breakpoint / pdb / skip in scope.",
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
            f"Loop: debug-cruft  ·  scope: {scope}  ·  {len(hits)} marker(s)"
            + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
