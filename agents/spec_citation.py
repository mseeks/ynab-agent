"""spec-citation loop (Many Hands Engineering, read-only).

Net-new and project-specific. This is a spec-driven codebase: docstrings carry
dense ``SPEC §N`` / ``SPEC §N.M`` cross-references that tie a module to the
design section it implements. When the spec is renumbered or a section is
dropped, those citations rot silently — a reader follows ``§9`` to the wrong
place, and the "why" link the project leans on breaks.

Signal: a pure scan. It builds the set of *valid* section ids from ``SPEC.md``
(every numbered heading, plus every ``§id`` the spec cites about itself, so a
prose-defined subsection like ``§0.5`` counts), then flags every ``§id`` in a
Python file whose id is not in that set — a dangling citation.

Action: a read-only agent verifies each dangling citation (the section may have
moved, been merged, or the cite may be a typo) and also looks for *semantic*
drift — a ``§N`` whose section no longer describes what the docstring claims. It
emits a three-bucket map — *Dangling* (the section id does not exist; name the
right one) / *Resolves* (a false positive — the id is valid, the scan missed it)
/ *Judgment-heavy* (exists but semantically drifted) — cite-or-omit.

Usage:
    python -m agents.spec_citation [scope]   # a path; default: src
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

# A section id like ``9``, ``4.2``, ``0.5``.
_SECTION_ID = r"\d+(?:\.\d+)?"
# A citation in code/prose: the section sign then an id (whitespace-tolerant).
_CITATION = re.compile(rf"§\s*({_SECTION_ID})")
# A numbered Markdown heading: ``## 4.2 Title`` / ``### 14. …``.
_HEADING = re.compile(rf"^#{{1,6}}\s+({_SECTION_ID})")


def valid_sections(spec_path: Path) -> set[str]:
    """The section ids ``SPEC.md`` defines (headings + its own §-citations).

    A subsection the spec only names in prose (``§0.5``) is still valid: the
    spec citing itself is taken as authoritative, so the loop never flags a
    living section just because it lacks its own ``##`` heading.
    """
    if not spec_path.exists():
        return set()
    ids: set[str] = set()
    for line in spec_path.read_text().splitlines():
        heading = _HEADING.match(line)
        if heading:
            ids.add(heading.group(1))
        ids.update(_CITATION.findall(line))
    return ids


def scan_spec_citations(
    target: Path, *, spec_path: Path | None = None
) -> list[Hit]:
    """Find ``§id`` citations in Python files with no such spec section."""
    spec = spec_path or (APP_ROOT / "SPEC.md")
    valid = valid_sections(spec)
    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            for section in _CITATION.findall(raw):
                if section not in valid:
                    snippet = raw.strip()[:SNIPPET_MAX]
                    hits.append(Hit(rel, lineno, f"§{section}", snippet))
    return sorted(hits, key=lambda h: (h.path, h.line, h.kind))


_SYSTEM_PROMPT = """\
You are the spec-citation loop for a spec-driven project. You are READ-ONLY: you
map citations that no longer resolve; you never edit. A human fixes what you
confirm.

Module docstrings cite design sections as `SPEC §N` / `SPEC §N.M`. You are given
a deterministic list of citations whose section id is NOT defined in `SPEC.md`
(neither a numbered heading nor a section the spec cites itself). For each, Read
the citing line and `SPEC.md` and sort it:

- DANGLING — the cited section does not exist: it was renumbered, merged, or the
  cite is a typo. Name the section that covers this, or say it is gone. Quote
  the citing line (file:line).
- RESOLVES — a false positive: the id IS valid and the scan missed it (e.g. an
  odd format). Show where it lives in SPEC.md.
- JUDGMENT-HEAVY — the id exists but the section no longer describes what the
  docstring claims (semantic drift). State the mismatch.

Beyond the list, you MAY note a citation that resolves numerically but points at
the wrong content (semantic drift) — that is the highest-value find.

HARD RULES:
- Cite or omit. Quote the citing line and the SPEC heading you checked.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} dangling SPEC citation(s) under "
        f"`{scope}`"
        + (f" (showing the first {MAX_HITS})" if truncated else "")
        + ":",
        "",
    ]
    for i, hit in enumerate(shown, start=1):
        lines.append(
            f"[{i}] {hit.path}:{hit.line}  (cites {hit.kind})  |  {hit.text}"
        )
    lines += [
        "",
        "Verify each against SPEC.md, then produce the three-bucket map.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the spec-citation loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: spec-citation  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_spec_citations(target)
    if not hits:
        report(
            [
                f"Loop: spec-citation  ·  scope: {scope}",
                "Deterministic signal: every SPEC citation resolves to a "
                "section.",
                "",
                "RESULT: PASS — no dangling spec citations.",
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
            f"Loop: spec-citation  ·  scope: {scope}  ·  "
            f"{len(hits)} citation(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
