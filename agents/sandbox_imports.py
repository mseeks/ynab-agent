"""sandbox-imports loop (Many Hands Engineering, read-only).

Net-new and Temporal-specific. A Temporal worker re-imports a workflow's module
graph inside a deterministic *sandbox*; whatever a workflow file (or an activity
module a workflow imports) pulls in *at module level* is re-executed there. The
heavy, non-deterministic stacks — the Pydantic AI model layer, the AgentMail
SDK, and this project's ``agentic``/``mail`` packages that wrap them — must not
enter that sandbox (this is the class of bug behind an earlier multi-hour hang:
a duplicate model class breaking a discriminated union on replay).

The discipline is: import those stacks *lazily, inside the activity body* (which
runs outside the sandbox) or under ``TYPE_CHECKING`` (never executed). This loop
flags any *runtime, module-level* import of a forbidden package in a file that
defines a ``@workflow.defn`` or ``@activity.defn``.

Signal: a pure AST scan. For each workflow/activity file it collects imports
that are module-level *and* runtime (excluding those inside a function body or
an ``if TYPE_CHECKING`` block, but INCLUDING the ``imports_passed_through()``
block, which is exactly what the sandbox re-runs) and keeps the forbidden ones.

Action: a read-only agent Reads each hit and sorts it — *Hazard* (a real
module-level import that will enter the sandbox; move it lazily into the
activity or under TYPE_CHECKING) / *Safe* (the scan over-matched — it is lazy,
type-only, or the file is not a sandboxed workflow) / *Judgment-heavy* —
cite-or-omit.

Usage:
    python -m agents.sandbox_imports [scope]   # a path; default: src
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
    from collections.abc import Iterator
    from pathlib import Path

# Only files that define a workflow or an activity feed the sandbox graph.
_SANDBOX_MARKERS = ("@workflow.defn", "@activity.defn")

# Packages that must never be re-imported inside a Temporal workflow sandbox:
# the model stack, the mail/YNAB SDKs, and this project's wrappers around them.
_FORBIDDEN = (
    "pydantic_ai",
    "agentmail",
    "httpx",
    "ynab_agent.agentic",
    "ynab_agent.mail",
    "ynab_agent.ynab",
)


def _forbidden_module(module: str | None) -> str | None:
    """Return the forbidden root a module belongs to, or None."""
    if module is None:
        return None
    for root in _FORBIDDEN:
        if module == root or module.startswith(root + "."):
            return root
    return None


def _runtime_module_imports(
    tree: ast.Module,
) -> Iterator[ast.Import | ast.ImportFrom]:
    """Yield module-level, runtime imports (skip funcs and TYPE_CHECKING).

    The ``with workflow.unsafe.imports_passed_through():`` block is module-level
    and IS re-run by the sandbox, so its imports are yielded; imports inside a
    function (the safe lazy pattern) and under ``if TYPE_CHECKING`` are not.
    """
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.With):
            for inner in node.body:
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    yield inner


def _imported_modules(
    node: ast.Import | ast.ImportFrom,
) -> list[str]:
    """The dotted module name(s) an import statement names."""
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.module else []
    return [alias.name for alias in node.names]


def scan_sandbox_imports(target: Path) -> list[Hit]:
    """Find forbidden runtime imports in workflow/activity files (signal)."""
    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        if not any(marker in text for marker in _SANDBOX_MARKERS):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for node in _runtime_module_imports(tree):
            for module in _imported_modules(node):
                root = _forbidden_module(module)
                if root is None:
                    continue
                snippet = lines[node.lineno - 1].strip()[:SNIPPET_MAX]
                hits.append(Hit(rel, node.lineno, root, snippet))
    return sorted(hits, key=lambda h: (h.path, h.line))


_SYSTEM_PROMPT = """\
You are the sandbox-imports loop for a Temporal + Pydantic AI project. You are
READ-ONLY: you map imports that would leak into a workflow sandbox; you never
edit. A human fixes what you confirm.

A Temporal worker re-imports a workflow's module graph in a deterministic
sandbox. Anything a `@workflow.defn` file — or a `@activity.defn` module a
workflow imports — pulls in AT MODULE LEVEL is re-run there. The model stack
(`pydantic_ai`), the mail SDK (`agentmail`), and this project's `agentic`/`mail`
wrappers MUST NOT enter the sandbox: import them lazily inside the activity body
(it runs outside the sandbox) or under `if TYPE_CHECKING` (never executed).

You are given a deterministic list of runtime, module-level imports of those
forbidden packages in workflow/activity files. Read each in context and sort it:

- HAZARD — a genuine module-level runtime import of a forbidden package. Say
  whether to move it into the activity body (lazy) or under TYPE_CHECKING, and
  quote the line.
- SAFE — a false positive: the import is actually inside a function (lazy),
  under `if TYPE_CHECKING`, or the file is not a real sandboxed workflow.
  Name which.
- JUDGMENT-HEAVY — module-level but plausibly harmless (a light, deterministic
  helper). State the tradeoff.

HARD RULES:
- Cite or omit. Quote the import line you Read, or drop the hit.
- The fix is real: prefer naming "lazy import in the activity" or TYPE_CHECKING.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} forbidden import(s) across "
        f"{files} workflow/activity file(s) under `{scope}`"
        + (f" (showing the first {MAX_HITS})" if truncated else "")
        + ":",
        "",
    ]
    for i, hit in enumerate(shown, start=1):
        lines.append(
            f"[{i}] {hit.path}:{hit.line}  (forbidden: {hit.kind})  |  "
            f"{hit.text}"
        )
    lines += [
        "",
        "Read each in context, then produce the three-bucket map.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the sandbox-imports loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: sandbox-imports  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_sandbox_imports(target)
    if not hits:
        report(
            [
                f"Loop: sandbox-imports  ·  scope: {scope}",
                "Deterministic signal: no forbidden module-level imports in "
                "workflow/activity files.",
                "",
                "RESULT: PASS — the model/mail stacks stay out of the sandbox.",
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
            f"Loop: sandbox-imports  ·  scope: {scope}  ·  "
            f"{len(hits)} import(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
