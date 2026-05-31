"""test-backfill loop (Many Hands Engineering, read-only).

Signal: a pure AST + reference scan for *public* top-level functions/classes
that are used in ``src/`` (so they are live, not dead) yet are **never named in
any test**. This is the tests-axis sibling of the dead-code loop, and the two
partition the space: a symbol referenced nowhere is *dead* (dead-code); one
referenced in ``src/`` but absent from ``tests/`` is *live but untested* (here).
The project already enforces line coverage, so this catches the gap coverage
can't see — public behaviour exercised only transitively, named by no test.

Action: a locked-down, read-only agent Reads each candidate and curates it under
a hard value bar into three buckets — *Worth testing* (quote the signature, say
what behaviour and why it matters, propose 1-4 plain-English cases) / *Skip*
(a trivial holder/stub/delegation, with the reason) / *Worth testing but hard*
(real value, high mocking cost). It writes nothing; you write the tests you
agree are worth it. No coverage-chasing: if the value can't be named, skip.

This is the Python analogue of Revisionist loop #4 (which parses vitest
coverage + git churn); here the deterministic signal is a self-contained
reference scan, matching this project's other AST loops.

Usage:
    python -m agents.test_backfill [scope]   # a path; default: src
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


def _public_top_level_defs(tree: ast.Module) -> list[tuple[str, int]]:
    """Public ``(name, lineno)`` module-level defs/classes (no leading _)."""
    out: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and not node.name.startswith("_"):
            out.append((node.name, node.lineno))
    return out


def _blob(root: Path) -> str:
    return "\n".join(p.read_text() for p in iter_python_files(root))


def _count(name: str, blob: str) -> int:
    return len(re.findall(rf"\b{re.escape(name)}\b", blob))


def scan_test_backfill(
    target: Path,
    *,
    src_root: Path | None = None,
    tests_root: Path | None = None,
) -> list[Hit]:
    """Find public symbols used in src but never named in tests (the signal).

    A candidate's definition contributes one whole-word occurrence of its name;
    ``src`` count above one means it is used somewhere in ``src`` (live, not
    dead), and a ``tests`` count of zero means no test names it. Conservative:
    a symbol mentioned even once in a test — however shallowly — is cleared.

    Args:
        target: The directory (or file) whose public defs are candidates.
        src_root: The ``src`` tree (the "is it live?" universe). Defaults to the
            project's ``src``; tests inject their own.
        tests_root: The ``tests`` tree (the "is it tested?" universe).
    """
    src_root = src_root or APP_ROOT / "src"
    tests_root = tests_root or APP_ROOT / "tests"
    src_blob = _blob(src_root)
    tests_blob = _blob(tests_root)

    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        for name, lineno in _public_top_level_defs(tree):
            if _count(name, src_blob) <= 1:
                continue  # dead or defined-only — the dead-code loop's job
            if _count(name, tests_blob) > 0:
                continue  # already named in a test
            try:
                rel = str(path.relative_to(APP_ROOT))
            except ValueError:
                rel = str(path)
            snippet = lines[lineno - 1].strip()[:SNIPPET_MAX]
            hits.append(Hit(rel, lineno, name, snippet))
    return sorted(hits, key=lambda h: (h.path, h.line))


_SYSTEM_PROMPT = """\
You are the test-backfill loop for a Python project (strict typed DDD on
Pydantic + Temporal). You are READ-ONLY: you map high-value missing tests; you
never write them. A human writes the ones worth it.

You are given a deterministic list of PUBLIC top-level functions/classes used in
`src/` but named in NO test. Read each and curate it under a HARD value bar into
exactly one bucket:

- WORTH TESTING — include ONLY if you can answer both in one sentence: (a) what
  behaviour the test verifies — quote the actual signature or branch you Read,
  in backticks (no quoted signature = you did not Read it = drop it); (b) why
  that behaviour matters to a caller. Then propose 1-4 concrete cases (positive,
  negative, edge) in plain English. No code.
- SKIP — not worth a test: a trivial data holder, a stubbed activity with no
  body, pure delegation, a wrapper where the mock would dominate. State the
  SPECIFIC reason — showing what you rejected is part of the map.
- WORTH TESTING BUT HARD — real value, but high cost (heavy mocking, Temporal/
  integration setup). Name the cost; the human decides whether to invest.

HARD RULES:
- Do NOT propose a test just to fill coverage. If you cannot articulate clear
  value, SKIP. Coverage-chasing is the failure mode this loop exists to avoid.
- Cite or omit: quote the signature/branch you actually Read, or drop the item.
- Be terse. This is a curated map, not an essay.

End with a one-line RESULT: "<X> worth · <Y> skip · <Z> hard"."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} public symbol(s) under `{scope}` "
        f"used in src but named in no test"
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
        "Read each, then curate the three-bucket map under the value bar.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the test-backfill loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: test-backfill  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_test_backfill(target)
    if not hits:
        report(
            [
                f"Loop: test-backfill  ·  scope: {scope}",
                "Deterministic signal: every public symbol is named in a test.",
                "",
                "RESULT: PASS — no untested public surface in scope.",
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
            f"Loop: test-backfill  ·  scope: {scope}  ·  "
            f"{len(hits)} symbol(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
