"""comment-debt loop (Many Hands Engineering, read-only).

Signal: a pure regex sweep for the canonical comment-debt markers — ``TODO``,
``FIXME``, ``HACK``, ``XXX``. Each is a deferred decision left in the code; left
unattended they accumulate into noise the next reader trips over.

Action: a locked-down, read-only agent Reads each hit in context and classifies
it into a strict three-bucket map — *Action now* / *Legitimately kept* /
*Judgment-heavy* — quoting the marker line it observed (cite-or-omit; no
confabulation). The loop writes nothing; you delete / lift / fix what you pick.

Mirrors the type-debt loop's shape (Revisionist loop #7), sharing the harness.

Usage:
    python -m agents.comment_debt [scope]   # scope is a path; default: src
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

# Canonical comment-debt markers, matched case-sensitively as whole words so a
# lowercase "todo" in prose or "HACKATHON" does not trip them.
_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bTODO\b"), "TODO"),
    (re.compile(r"\bFIXME\b"), "FIXME"),
    (re.compile(r"\bHACK\b"), "HACK"),
    (re.compile(r"\bXXX\b"), "XXX"),
)


def scan_comment_debt(target: Path) -> list[Hit]:
    """Sweep ``target`` for comment-debt markers (the loop's signal)."""
    return sweep(target, _MARKERS)


_SYSTEM_PROMPT = """\
You are the comment-debt loop for a Python project. You are READ-ONLY: you map
deferred-decision markers (TODO / FIXME / HACK / XXX); you never edit. A human
acts on your map.

You are given a deterministic list of marker hits. For each, Read the
surrounding code and classify it into exactly one bucket:

- ACTION NOW — a real, addressable item. State WHAT it asks, WHY it matters,
  and the concrete ACTION, each in one sentence. Quote the marker line with
  file:line.
- LEGITIMATELY KEPT — a verified intentional marker: a compatibility warning,
  a documented design rationale, or an aspirational tag with a real reason.
  State the SPECIFIC reason.
- JUDGMENT-HEAVY — a real item whose fix is non-trivial. Describe the tradeoff.

HARD RULES:
- Cite or omit. Quote only marker lines you actually observed via Read/Grep.
  Never invent a marker, a line number, or context that isn't there.
- Do not editorialize about whether comments are "good"; only triage the
  deferred work each marker names.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} comment-debt marker(s) under "
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
    """Run the comment-debt loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [f"Loop: comment-debt  ·  scope: {scope}", f"Not found: {target}"]
        )
        return 1

    hits = scan_comment_debt(target)
    if not hits:
        report(
            [
                f"Loop: comment-debt  ·  scope: {scope}",
                "Deterministic signal: 0 comment-debt markers found.",
                "",
                "RESULT: PASS — no TODO / FIXME / HACK / XXX in scope.",
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
            f"Loop: comment-debt  ·  scope: {scope}  ·  {len(hits)} marker(s)"
            + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
