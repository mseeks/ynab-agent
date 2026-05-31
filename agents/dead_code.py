"""dead-code loop (Many Hands Engineering, read-only).

Signal: a pure AST scan for *top-level* functions and classes whose name never
appears — as a whole word — anywhere else in ``src/`` or ``tests/``. Nothing
references them by name: no importer, no call, no type annotation, no registry
entry. A growing pure-core library quietly accretes such orphans.

Action: a locked-down, read-only agent Reads each candidate and sorts it into a
strict three-bucket map — *Delete now* (genuinely unreachable) / *Reachable*
(a framework false positive — a registered Temporal entrypoint, a discriminated-
union member, a dynamic reference) / *Judgment-heavy* (a deliberate extension
point) — quoting the Grep/Glob it actually ran (cite-or-omit). The loop deletes
nothing; you act on its map.

This is the Python analogue of Revisionist loop #3 (which shells out to
``knip``); the deterministic candidate list is a self-contained AST + ref scan,
matching this project's other sweep loops. The word-boundary reference count is
what keeps it low-noise: a registered activity, a union member, and a field type
all *are* references, so they are never flagged.

Usage:
    python -m agents.dead_code [scope]   # a path; default: src
"""

from __future__ import annotations

import ast
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

# The reference universe: a symbol used anywhere in src or tests is not dead.
_REF_ROOTS = ("src", "tests")


def _top_level_defs(tree: ast.Module) -> list[tuple[str, int]]:
    """Module-level def/class names with line numbers (dunders excluded)."""
    out: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            name = node.name
            if name.startswith("__") and name.endswith("__"):
                continue
            out.append((name, node.lineno))
    return out


def _exported_names(tree: ast.Module) -> set[str]:
    """Names listed in a module's ``__all__`` (explicit public API)."""
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = (t for t in node.targets if isinstance(t, ast.Name))
        if not any(t.id == "__all__" for t in targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and isinstance(
                    element.value, str
                ):
                    names.add(element.value)
    return names


def _reference_blob(roots: tuple[Path, ...]) -> str:
    """All source text references are searched against (src + tests)."""
    chunks: list[str] = []
    for root in roots:
        for path in iter_python_files(root):
            chunks.append(path.read_text())
    return "\n".join(chunks)


def scan_dead_code(
    target: Path, *, reference_roots: tuple[Path, ...] | None = None
) -> list[Hit]:
    """Find top-level defs/classes unreferenced by name in src+tests (signal).

    A candidate's definition contributes exactly one whole-word occurrence of
    its name (right after ``def``/``class``); any second occurrence anywhere —
    a call, an import, an annotation, a registry list, even an in-file use —
    pushes the count past one and clears it. So a flagged symbol genuinely has
    zero references. Conservative by construction: shared names hide each other.

    Args:
        target: The directory (or file) whose top-level defs are candidates.
        reference_roots: Roots forming the reference universe. Defaults to the
            project's ``src`` and ``tests`` trees; tests inject their own.
    """
    roots = reference_roots or tuple(APP_ROOT / r for r in _REF_ROOTS)
    blob = _reference_blob(roots)
    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        exported = _exported_names(tree)
        lines = text.splitlines()
        for name, lineno in _top_level_defs(tree):
            if name in exported:
                continue
            occurrences = len(re.findall(rf"\b{re.escape(name)}\b", blob))
            if occurrences > 1:
                continue
            try:
                rel = str(path.relative_to(APP_ROOT))
            except ValueError:
                rel = str(path)
            snippet = lines[lineno - 1].strip()[:SNIPPET_MAX]
            hits.append(Hit(rel, lineno, name, snippet))
    return sorted(hits, key=lambda h: (h.path, h.line))


_SYSTEM_PROMPT = """\
You are the dead-code loop for a Python project (strict typed DDD on Pydantic +
Temporal). You are READ-ONLY: you map unreferenced symbols; you never delete. A
human acts on your map.

You are given a deterministic list of top-level functions/classes whose name
does not appear — as a whole word — anywhere else in `src/` or `tests/`. Nothing
references them by name. The scan is conservative but framework-blind in spots,
so VERIFY each with Read/Grep/Glob, then sort it into exactly one bucket:

- DELETE NOW — genuinely unreachable: no importer, no registry entry, no dynamic
  use; deleting it changes nothing. Quote the Grep/Glob you ran and its result.
- REACHABLE — actually used, just not via a bare-name reference the scan counts:
  a Temporal workflow/activity reached through a registry list, a Pydantic model
  used only as a discriminated-union member or field type, a public entrypoint,
  or a dynamic/string reference. Name the exact mechanism.
- JUDGMENT-HEAVY — dead today, but a deliberate near-term extension point (an
  event/effect type the spine will emit later, a smart constructor). State the
  tradeoff; the human decides keep-vs-delete.

HARD RULES:
- Cite or omit. State the exact Grep pattern / Glob you ran and what it
  returned. "I checked, it's unused" is NOT evidence — omit it if uncertain.
- A symbol flagged here has zero references even inside its own module (in-file
  uses are counted), so do not rule it out as "used locally" without evidence.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} unreferenced symbol(s) under "
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
        "Read each, verify with Grep/Glob, then produce the three-bucket map.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the dead-code loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: dead-code  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_dead_code(target)
    if not hits:
        report(
            [
                f"Loop: dead-code  ·  scope: {scope}",
                "Deterministic signal: every top-level symbol is referenced.",
                "",
                "RESULT: PASS — no unreferenced symbols in scope.",
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
            f"Loop: dead-code  ·  scope: {scope}  ·  "
            f"{len(hits)} symbol(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
