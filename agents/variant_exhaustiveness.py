"""variant-exhaustiveness loop (Many Hands Engineering, read-only).

Net-new and project-specific. The domain is built on discriminated unions —
``Effect``, ``InboundSignal``, ``LearningEvent``, ``DispatchDecision``,
the allocation unions — each ``Annotated[A | B | …, Field(discriminator=…)]``.
The classic feature-growth bug is adding a member to such a union and forgetting
to handle it at a call site: the new effect/signal/state silently no-ops on the
branch that should act on it. mypy's ``assert_never`` catches a missing branch
*inside an exhaustive match*, but not a union whose member no code dispatches on
at all.

Signal: a pure AST scan. It collects every discriminated-union member class, and
separately every class named in a ``case C(…)`` or an ``isinstance(_, C)`` check
check across ``src/``. A member appearing in neither is flagged — nothing
structurally dispatches on it.

Action: a read-only agent Reads each flagged member and sorts it — *Unwired*
(genuinely needs a ``case``/branch; the feature is half-wired) / *Wired*
(dispatched generically — by the Pydantic data converter on its discriminant, or
handled via a base type) / *Judgment-heavy* — cite-or-omit.

Usage:
    python -m agents.variant_exhaustiveness [scope]   # a path; default: src
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


def _name_is(node: ast.expr, name: str) -> bool:
    """Whether ``node`` is the bare ``name`` or an attribute ending in it."""
    if isinstance(node, ast.Name):
        return node.id == name
    return isinstance(node, ast.Attribute) and node.attr == name


def _is_field_discriminator(node: ast.expr) -> bool:
    """Whether ``node`` is a ``Field(discriminator=…)`` call."""
    return (
        isinstance(node, ast.Call)
        and _name_is(node.func, "Field")
        and any(kw.arg == "discriminator" for kw in node.keywords)
    )


def _discriminated_union(value: ast.expr) -> ast.expr | None:
    """The union ``BinOp`` of an ``Annotated[A | B, Field(discriminator=…)]``.

    Returns the ``A | B | …`` node when ``value`` is a discriminated union, else
    ``None``.
    """
    if not (
        isinstance(value, ast.Subscript) and _name_is(value.value, "Annotated")
    ):
        return None
    sliced = value.slice
    if not isinstance(sliced, ast.Tuple) or not sliced.elts:
        return None
    first, *rest = sliced.elts
    if isinstance(first, ast.BinOp) and any(
        _is_field_discriminator(meta) for meta in rest
    ):
        return first
    return None


def _union_member_names(node: ast.expr) -> list[str]:
    """The class names in a ``A | B | C`` union ``BinOp``."""
    names: list[str] = []

    def walk(inner: ast.expr) -> None:
        if isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr):
            walk(inner.left)
            walk(inner.right)
        elif isinstance(inner, ast.Name):
            names.append(inner.id)

    walk(node)
    return names


def _assign_value(node: ast.stmt) -> ast.expr | None:
    """The RHS of a module-level ``X = …`` or ``X: T = …`` assignment."""
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _isinstance_names(arg: ast.expr) -> set[str]:
    """Class names in ``isinstance(_, C)`` / ``isinstance(_, (C, D))``."""
    if isinstance(arg, ast.Name):
        return {arg.id}
    if isinstance(arg, ast.Tuple):
        return {e.id for e in arg.elts if isinstance(e, ast.Name)}
    return set()


def _dispatched_names(roots: tuple[Path, ...]) -> set[str]:
    """Classes structurally dispatched on (``case C`` / ``isinstance``)."""
    names: set[str] = set()
    for root in roots:
        for path in iter_python_files(root):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.MatchClass) and isinstance(
                    node.cls, ast.Name
                ):
                    names.add(node.cls.id)
                elif (
                    isinstance(node, ast.Call)
                    and _name_is(node.func, "isinstance")
                    and len(node.args) >= 2
                ):
                    names |= _isinstance_names(node.args[1])
    return names


def scan_variant_exhaustiveness(
    target: Path, *, reference_roots: tuple[Path, ...] | None = None
) -> list[Hit]:
    """Find discriminated-union members nothing dispatches on (the signal)."""
    roots = reference_roots or (APP_ROOT / "src",)
    dispatched = _dispatched_names(roots)
    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for node in tree.body:
            value = _assign_value(node)
            union = _discriminated_union(value) if value is not None else None
            if union is None:
                continue
            snippet = lines[node.lineno - 1].strip()[:SNIPPET_MAX]
            for member in _union_member_names(union):
                if member not in dispatched:
                    hits.append(Hit(rel, node.lineno, member, snippet))
    return sorted(hits, key=lambda h: (h.path, h.line, h.kind))


_SYSTEM_PROMPT = """\
You are the variant-exhaustiveness loop for a strict typed DDD project (Pydantic
discriminated unions + Temporal). You are READ-ONLY: you map union members that
nothing dispatches on; you never edit. A human acts on your map.

The domain models choices as discriminated unions (`Effect`, `InboundSignal`,
`LearningEvent`, `DispatchDecision`, the allocation unions). The growth bug is
adding a member and forgetting to handle it — the new variant silently no-ops on
the branch that should act on it.

You are given a deterministic list of union members whose class name appears in
NO `case C(...)` pattern and NO `isinstance(_, C)` check anywhere in `src/`.
Read each (the member class and the call sites that fold its union) and sort it:

- UNWIRED — a real gap: code folding this union has no branch for this member,
  so it is silently dropped. Name the call site that needs the `case`/branch and
  quote it.
- WIRED — dispatched without a structural match: the Pydantic data converter
  routes it by its discriminant (serialization only), or a base-type handler
  covers it. Name the exact mechanism.
- JUDGMENT-HEAVY — plausibly fine but worth a human glance (e.g. a member only
  ever produced, never consumed). State the tradeoff.

HARD RULES:
- Cite or omit. Quote the call site (or its absence, with the Grep you ran).
- "Probably handled" is not evidence — verify the fold sites with Read/Grep.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} undispatched union member(s) in "
        f"`{scope}`"
        + (f" (showing the first {MAX_HITS})" if truncated else "")
        + ":",
        "",
    ]
    for i, hit in enumerate(shown, start=1):
        lines.append(
            f"[{i}] {hit.path}:{hit.line}  (member: {hit.kind})  |  {hit.text}"
        )
    lines += [
        "",
        "Read each member and its union's fold sites, then map the buckets.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the variant-exhaustiveness loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: variant-exhaustiveness  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_variant_exhaustiveness(target)
    if not hits:
        report(
            [
                f"Loop: variant-exhaustiveness  ·  scope: {scope}",
                "Deterministic signal: every union member is dispatched on.",
                "",
                "RESULT: PASS — no undispatched discriminated-union members.",
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
            f"Loop: variant-exhaustiveness  ·  scope: {scope}  ·  "
            f"{len(hits)} member(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
