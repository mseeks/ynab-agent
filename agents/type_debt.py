"""type-debt loop (Many Hands Engineering, read-only).

Signal: a pure regex sweep for Python's typing escape hatches — ``Any``
annotations, :func:`typing.cast`, and ``# type: ignore`` / ``# mypy:`` /
``# pyright:`` suppressions. These weaken the static guarantees that make
illegal states unrepresentable, so they earn scrutiny.

Action: a locked-down, read-only agent Reads each hit in context and classifies
it into a strict three-bucket map — *Tightenable now* / *Legitimately needed* /
*Judgment-heavy* — naming the concrete narrower type where one exists, and
quoting what it observed (cite-or-omit; no confabulation, no hypotheticals).

The loop writes nothing; you tighten what you choose.

Usage:
    python -m agents.type_debt [scope]   # scope is a path; default: src
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
    (re.compile(r"#\s*type:\s*ignore"), "type-ignore"),
    (re.compile(r"#\s*mypy:\s*(?:ignore-errors|disable-error-code)"), "mypy"),
    (re.compile(r"#\s*pyright:\s*ignore"), "pyright"),
    (re.compile(r"\bcast\s*\("), "cast"),
    (re.compile(r"(?::|->|\[|,|\|)\s*Any\b|\bAny\s*(?:\]|,|\|)"), "any"),
)


def scan_type_debt(target: Path) -> list[Hit]:
    """Sweep ``target`` for typing escape hatches (the loop's signal)."""
    return sweep(target, _MARKERS)


_SYSTEM_PROMPT = """\
You are the type-debt loop for a Python project that uses strict mypy and
Pydantic v2 to make illegal states unrepresentable. You are READ-ONLY: you
map typing escape hatches; you never edit. A human acts on your map.

You are given a deterministic list of markers (Any annotations, typing.cast,
and type/mypy/pyright suppressions). For each, Read the surrounding code and
classify it into exactly one bucket:

- TIGHTENABLE NOW — a real, mechanical fix. You MUST name the concrete
  narrower type (e.g. "use Decimal, not Any"; "annotate -> Transaction").
  Quote the offending line with file:line.
- LEGITIMATELY NEEDED — a verified boundary that genuinely needs the loose
  type: an external/untyped API, a dynamic protocol, a documented
  mypy/Pydantic limitation. State the SPECIFIC reason.
- JUDGMENT-HEAVY — real debt, but the fix is non-trivial (a cascade, or it
  reshapes an API surface). Describe the tradeoff.

HARD RULES:
- Cite or omit. Quote only lines you actually observed via Read/Grep. Never
  invent a marker, a line number, or a type that isn't there.
- No purity-for-purity's-sake and no hypothetical refactors. If a loose type
  is fine, say so under LEGITIMATELY NEEDED — do not manufacture work.
- Be terse. This is a map, not an essay.

KNOWN CONTEXT:
- The domain core is pure, frozen Pydantic v2; `Any` INSIDE the domain is
  debt. `Any` may be legitimate only at the Temporal/MCP boundary.
- The claude_agent_sdk is fully typed (ships py.typed), so casts around it
  are usually unnecessary.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} type-debt marker(s) under "
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
    """Run the type-debt loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report([f"Loop: type-debt  ·  scope: {scope}", f"Not found: {target}"])
        return 1

    hits = scan_type_debt(target)
    if not hits:
        report(
            [
                f"Loop: type-debt  ·  scope: {scope}",
                "Deterministic signal: 0 type-debt markers found.",
                "",
                "RESULT: PASS — no Any / cast / type: ignore in scope.",
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
            f"Loop: type-debt  ·  scope: {scope}  ·  {len(hits)} marker(s)"
            + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
