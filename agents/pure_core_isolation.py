"""pure-core-isolation loop (Many Hands Engineering, read-only).

Net-new and project-specific. The architecture rests on a hard seam: the *pure
core* (`domain/`, `learn/`, `policy/`, `ingest/`, `budget/`) is deterministic,
side-effect-free, and trivially testable — it reasons over values and never
performs I/O. The *spine* (`workflow/`) and the *adapters* (`agentic/`, `mail/`,
`ynab/`, `webhook/`, `telemetry`) own all I/O and framework coupling. The whole
testability + replay-determinism story depends on the core never reaching
"downward" into the spine or "outward" into an SDK.

`sandbox-imports` guards a narrower thing — what enters the *Temporal sandbox*
via `@workflow.defn`/`@activity.defn` files. This loop guards the *core*: it
flags any import (module-level, nested, OR under `TYPE_CHECKING`) of an I/O or
framework stack from inside a pure-layer file, because a pure module should not
even type against the spine.

Signal: a pure AST scan. For each file under a pure layer it walks every import
and keeps those naming a forbidden root (`temporalio`, the model/mail/web SDKs,
or the project's own I/O packages). `pydantic`/`pydantic_settings` are allowed —
the frozen domain is built on them.

Action: a read-only agent Reads each hit and sorts it — *Leak* (a real layering
inversion; push the import down into the spine/activity, or invert the
dependency) / *Clean* (pure→pure, or the scan mis-classified the file) /
*Judgment-heavy* — cite-or-omit.

Usage:
    python -m agents.pure_core_isolation [scope]   # a path; default: src
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

# The pure layers: deterministic, I/O-free, the testable heart of the system.
_PURE_LAYERS = ("domain", "learn", "policy", "ingest", "budget")
_PURE_PREFIXES = tuple(f"src/ynab_agent/{layer}/" for layer in _PURE_LAYERS)

# Stacks a pure module must never import — the spine, the SDKs, the adapters,
# and the project's own I/O packages. `pydantic` is deliberately NOT here: the
# frozen domain is built on it.
_FORBIDDEN = (
    "temporalio",
    "pydantic_ai",
    "agentmail",
    "httpx",
    "fastapi",
    "uvicorn",
    "svix",
    "ynab_agent.workflow",
    "ynab_agent.agentic",
    "ynab_agent.mail",
    "ynab_agent.ynab",
    "ynab_agent.webhook",
    "ynab_agent.telemetry",
)


def _is_pure_layer_file(rel: str) -> bool:
    """Whether ``rel`` (a path relative to the project root) is pure-layer."""
    return rel.startswith(_PURE_PREFIXES)


def _forbidden_module(module: str | None) -> str | None:
    """Return the forbidden root a module belongs to, or None."""
    if module is None:
        return None
    for root in _FORBIDDEN:
        if module == root or module.startswith(root + "."):
            return root
    return None


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    """The dotted module name(s) an import statement names."""
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.module else []
    return [alias.name for alias in node.names]


def scan_pure_core_isolation(
    target: Path, *, root: Path | None = None
) -> list[Hit]:
    """Find forbidden imports inside pure-layer files (the signal).

    Walks *every* import in a pure-layer module — module-level, nested in a
    function, and under ``TYPE_CHECKING`` alike — since the pure core should not
    even type against the spine or an SDK.

    Args:
        target: The directory (or file) to scan.
        root: The project root paths are made relative to (so a layer is
            recognised by its ``src/ynab_agent/<layer>/`` prefix). Defaults to
            the real project root; tests inject a temporary tree.
    """
    base = root or APP_ROOT
    hits: list[Hit] = []
    for path in iter_python_files(target):
        try:
            rel = str(path.relative_to(base))
        except ValueError:
            continue
        if not _is_pure_layer_file(rel):
            continue
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _imported_modules(node):
                forbidden = _forbidden_module(module)
                if forbidden is None:
                    continue
                snippet = lines[node.lineno - 1].strip()[:SNIPPET_MAX]
                hits.append(Hit(rel, node.lineno, forbidden, snippet))
    return sorted(hits, key=lambda h: (h.path, h.line))


_SYSTEM_PROMPT = """\
You are the pure-core-isolation loop for a strict typed DDD project (Pydantic +
Temporal). You are READ-ONLY: you map layering violations; you never edit. A
human fixes what you confirm.

The pure core — `domain/`, `learn/`, `policy/`, `ingest/`, `budget/` — is
deterministic and I/O-free: it reasons over values and is trivially testable.
All I/O and framework coupling lives in the spine (`workflow/`) and adapters
(`agentic/`, `mail/`, `ynab/`, `webhook/`, `telemetry`). The core must never
import the spine, an SDK (`temporalio`, `pydantic_ai`, `agentmail`, `httpx`,
`fastapi`, …), or an adapter package — not even under `TYPE_CHECKING`.
(`pydantic` itself is allowed; the frozen domain is built on it.)

You are given a deterministic list of forbidden imports found inside pure-layer
files. Read each in context and sort it:

- LEAK — a genuine layering inversion: a pure module imports the spine, an SDK,
  or an adapter. Say how to fix it (push the import into the activity/spine,
  pass the value in, or invert the dependency via a Protocol); quote the line.
- CLEAN — a false positive: the import is pure→pure, or the file is not actually
  in the pure core. Name why.
- JUDGMENT-HEAVY — a borderline case (e.g. a type-only import that is hard to
  avoid). State the tradeoff.

HARD RULES:
- Cite or omit. Quote the import line you Read, or drop the hit.
- The fix is real: name where the import belongs instead.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} forbidden import(s) across "
        f"{files} pure-layer file(s) under `{scope}`"
        + (f" (showing the first {MAX_HITS})" if truncated else "")
        + ":",
        "",
    ]
    for i, hit in enumerate(shown, start=1):
        lines.append(
            f"[{i}] {hit.path}:{hit.line}  (forbidden: {hit.kind})  |  "
            f"{hit.text}"
        )
    lines += ["", "Read each in context, then produce the three-bucket map."]
    return "\n".join(lines)


async def main() -> int:
    """Run the pure-core-isolation loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: pure-core-isolation  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_pure_core_isolation(target)
    if not hits:
        report(
            [
                f"Loop: pure-core-isolation  ·  scope: {scope}",
                "Deterministic signal: no forbidden imports in pure-layer "
                "files.",
                "",
                "RESULT: PASS — the pure core stays free of I/O and framework "
                "coupling.",
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
            f"Loop: pure-core-isolation  ·  scope: {scope}  ·  "
            f"{len(hits)} import(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
