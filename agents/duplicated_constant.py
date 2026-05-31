"""duplicated-constant loop (Many Hands Engineering, read-only).

Signal: a pure scan for the same numeric literal re-typed on a *rule-bearing*
line (a comparison, a ``timedelta``, a Pydantic ``Field`` bound, or a ``Money``
construction) at two or more distinct sites. A domain constant typed in two
places is one edit away from a silent rules bug.

Action: a locked-down, read-only agent Reads every site of each cluster and
classifies it into a strict three-bucket map — *Centralise now* (name a concrete
constant and its home) / *Legitimately repeated* (coincidental or a standard
value) / *Judgment-heavy* — quoting every site (cite-or-omit). The loop writes
nothing; you centralise what you choose.

This is MHE's behaviour/boundary axis (Revisionist loop #11), distinct from
type-debt (which finds weakened types).

Usage:
    python -m agents.duplicated_constant [scope]   # a path; default: src
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
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

# A standalone numeric literal (not part of an identifier or a dotted attr).
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
# Lines where a number carries a domain rule, not incidental bookkeeping.
_RULE_LINE = re.compile(
    r"[<>]=?|==|!=|\btimedelta\(|\bField\(|"
    r"\bfrom_currency\(|\bfrom_milliunits\("
)
# Trivial literals that recur everywhere and mean nothing on their own.
_TRIVIAL = {"0", "1"}
_MIN_SITES = 2


def scan_duplicated_constants(target: Path) -> list[Hit]:
    """Find numeric literals re-typed on rule lines at 2+ sites (the signal)."""
    by_value: dict[str, list[Hit]] = defaultdict(list)
    for path in iter_python_files(target):
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
            if not _RULE_LINE.search(raw):
                continue
            snippet = raw.strip()[:SNIPPET_MAX]
            for match in _NUMBER.finditer(raw):
                value = match.group(1)
                if value in _TRIVIAL:
                    continue
                by_value[value].append(Hit(rel, lineno, value, snippet))

    hits: list[Hit] = []
    for value_hits in by_value.values():
        sites = {(h.path, h.line) for h in value_hits}
        if len(sites) >= _MIN_SITES:
            hits.extend(value_hits)
    return sorted(hits, key=lambda h: (h.kind, h.path, h.line))


_SYSTEM_PROMPT = """\
You are the duplicated-constant loop for a Python project. You are READ-ONLY:
you map re-typed domain constants; you never edit. A human acts on your map.

You are given numeric literals that recur on rule-bearing lines at 2+ sites,
grouped by value (the `(kind)` field is the value). For each cluster, Read every
site and classify it into exactly one bucket:

- CENTRALISE NOW — the SAME domain constant re-typed at multiple sites. Name a
  concrete constant and where it should live; quote EVERY site (file:line).
- LEGITIMATELY REPEATED — coincidental values, a standard/library value, a bare
  year, or intentional prose. State the SPECIFIC reason.
- JUDGMENT-HEAVY — a real duplication, but centralising would couple modules
  that should not import each other. Describe the tradeoff.

HARD RULES:
- Cite or omit. Quote every site you actually observed, or drop the cluster.
  Never invent a site.
- A value appearing in a named-constant definition plus one inline use is a real
  duplication (the inline use should reference the constant).
- Be terse. This is a map, not an essay.

KNOWN CONTEXT:
- Money is integer milliunits; 1000 (milliunits/unit) and 100 (percent) are
  already named constants in the domain — flag only genuinely re-typed ones.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    clusters = len({h.kind for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} site(s) across {clusters} "
        f"value cluster(s) under `{scope}`"
        + (f" (showing the first {MAX_HITS})" if truncated else "")
        + ":",
        "",
    ]
    for i, hit in enumerate(shown, start=1):
        lines.append(
            f"[{i}] {hit.path}:{hit.line}  (={hit.kind})  |  {hit.text}"
        )
    lines += [
        "",
        "Group by value, Read each site, then produce the three-bucket map.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the duplicated-constant loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: duplicated-constant  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_duplicated_constants(target)
    if not hits:
        report(
            [
                f"Loop: duplicated-constant  ·  scope: {scope}",
                "Deterministic signal: no constant re-typed at 2+ rule sites.",
                "",
                "RESULT: PASS — no duplicated rule constants in scope.",
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
            f"Loop: duplicated-constant  ·  scope: {scope}  ·  "
            f"{len(hits)} site(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
