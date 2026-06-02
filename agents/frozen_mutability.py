"""frozen-mutability loop (Many Hands Engineering, read-only).

Net-new and project-specific — it guards the SPEC's substrate ("frozen, closed
domain models, on which illegal states are made unrepresentable"). Pydantic's
``frozen=True`` stops attribute *reassignment*, but a field typed with a mutable
container (``list`` / ``dict`` / ``set``) is still mutable *in place*: a holder
of the reference can ``.append`` / ``[k] = v`` / ``.add`` and quietly rewrite a
value that is supposed to be immutable. The frozen guarantee leaks. Immutable
domain models use ``tuple`` / ``Mapping`` / ``frozenset`` instead — and it
matters most for state carried across a workflow ``continue-as-new``, where a
mutated "value" desyncs the durable history.

Signal: a base-class-aware AST scan. It first computes the set of frozen classes
— anything subclassing ``Frozen`` (the ``domain/base.py`` base) or declaring
``frozen=True``, closed transitively so a subclass-of-a-frozen-subclass counts
too (a naive ``frozen=True`` regex misses ~123 of the ~127 models, which inherit
it). It then flags each frozen-class field annotated with a mutable head
(``list`` / ``dict`` / ``set``, even under ``X | None`` / ``Optional[...]``).

Action: a locked-down, read-only agent Reads each field and sorts it — *Mutable*
(change it to ``tuple`` / ``Mapping`` / ``frozenset``; quote the line) /
*Immutable* (a false positive: already immutable, a ``ClassVar``, or not a
frozen domain model) / *Judgment-heavy* (a deliberate mutable cache that is not
durable domain state) — cite-or-omit. The loop writes nothing.

Usage:
    python -m agents.frozen_mutability [scope]   # a path; default: src
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

# The immutable base every domain model inherits (domain/base.py).
_FROZEN_BASE = "Frozen"
# Mutable container heads that defeat a frozen model's immutability.
_MUTABLE_HEADS = frozenset({"list", "dict", "set", "List", "Dict", "Set"})
# Subscript heads whose *arguments* carry the real type (recurse into them).
_WRAPPERS = frozenset({"Optional", "Union"})


def _name_of(node: ast.expr) -> str | None:
    """The simple name of a Name/Attribute node (``dict``, ``Mapping``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _mutable_head(annotation: ast.expr) -> str | None:
    """Return the mutable container head of an annotation, or None.

    Recurses through ``X | Y`` unions and ``Optional[...]`` / ``Union[...]`` so
    ``dict[...] | None`` is caught, but does not descend into the *arguments* of
    an immutable head (``tuple[list[int], ...]`` is an immutable tuple).
    """
    if isinstance(annotation, ast.Subscript):
        head = _name_of(annotation.value)
        if head in _MUTABLE_HEADS:
            return head
        if head in _WRAPPERS:
            sl = annotation.slice
            members = sl.elts if isinstance(sl, ast.Tuple) else [sl]
            for member in members:
                found = _mutable_head(member)
                if found is not None:
                    return found
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id if annotation.id in _MUTABLE_HEADS else None
    if isinstance(annotation, ast.BinOp) and isinstance(
        annotation.op, ast.BitOr
    ):
        return _mutable_head(annotation.left) or _mutable_head(annotation.right)
    return None


def _declares_frozen(node: ast.ClassDef) -> bool:
    """Whether a class declares ``frozen=True`` (kwarg or model_config)."""
    for kw in node.keywords:
        if (
            kw.arg == "frozen"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
        ):
            return True
    for stmt in node.body:
        if not (
            isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
        ):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "model_config"
            for t in stmt.targets
        ):
            continue
        for kw in stmt.value.keywords:
            if (
                kw.arg == "frozen"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                return True
    return False


def _base_names(node: ast.ClassDef) -> set[str]:
    """The simple names of a class's bases."""
    return {name for b in node.bases if (name := _name_of(b)) is not None}


def _frozen_class_names(classes: list[ast.ClassDef]) -> set[str]:
    """Names of frozen classes, closed transitively over the given classes.

    Seeds on a ``Frozen`` base or a ``frozen=True`` declaration, then adds any
    class whose base is itself frozen (subclass-of-a-frozen-subclass).
    """
    bases_by_name: dict[str, set[str]] = {}
    frozen: set[str] = set()
    for node in classes:
        bases = _base_names(node)
        bases_by_name.setdefault(node.name, set()).update(bases)
        if _FROZEN_BASE in bases or _declares_frozen(node):
            frozen.add(node.name)
    changed = True
    while changed:
        changed = False
        for name, bases in bases_by_name.items():
            if name not in frozen and bases & frozen:
                frozen.add(name)
                changed = True
    return frozen


def scan_frozen_mutability(target: Path) -> list[Hit]:
    """Find mutable-container fields on frozen models (the signal).

    Args:
        target: A file or directory to scan.

    Returns:
        Hits ordered by path then line.
    """
    parsed: list[tuple[str, list[str], ast.Module]] = []
    all_classes: list[ast.ClassDef] = []
    for path in iter_python_files(target):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        parsed.append((rel, text.splitlines(), tree))
        all_classes.extend(
            n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
        )

    frozen = _frozen_class_names(all_classes)

    hits: list[Hit] = []
    for rel, lines, tree in parsed:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef) and node.name in frozen):
                continue
            for stmt in node.body:
                if not (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                ):
                    continue
                head = _mutable_head(stmt.annotation)
                if head is None:
                    continue
                snippet = lines[stmt.lineno - 1].strip()[:SNIPPET_MAX]
                hits.append(Hit(rel, stmt.lineno, head.lower(), snippet))
    return sorted(hits, key=lambda h: (h.path, h.line))


_SYSTEM_PROMPT = """\
You are the frozen-mutability loop for a strict DDD project on Pydantic +
Temporal. You are READ-ONLY: you map mutable fields on frozen domain models; you
never edit. A human fixes what you confirm.

Domain models are `frozen=True` so a new state is a new value, never a mutation.
But `frozen=True` only blocks attribute REASSIGNMENT — a field typed `list`,
`dict`, or `set` is still mutable IN PLACE (`.append`, `d[k] = v`), so the
immutability guarantee leaks. Immutable models use `tuple`, `Mapping`, and
`frozenset`. This matters most for state carried across a workflow
`continue-as-new`, where a mutated "value" desyncs the durable history.

You are given a deterministic list of fields, on classes that subclass `Frozen`
(or declare `frozen=True`), whose type is a mutable container. Read each in
context and sort it into exactly one bucket:

- MUTABLE — a genuine mutable field on a frozen domain model. Name the immutable
  replacement (`list[X]`→`tuple[X, ...]`, `dict[K,V]`→`Mapping[K,V]`,
  `set[X]`→`frozenset[X]`) and quote the line.
- IMMUTABLE — a false positive: the field is already immutable, it is a
  `ClassVar`/private/non-field, or the class is not really a frozen domain model
  (the scan over-reached on the name). Name which.
- JUDGMENT-HEAVY — a deliberate mutable buffer that is NOT durable domain
  state and never crosses a `continue-as-new`. State the tradeoff.

HARD RULES:
- Cite or omit. Quote the field line you Read, or drop the hit. Never invent.
- Prefer naming the exact immutable replacement type.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} mutable field(s) on frozen "
        f"model(s) across {files} file(s) under `{scope}`"
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
    """Run the frozen-mutability loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: frozen-mutability  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_frozen_mutability(target)
    if not hits:
        report(
            [
                f"Loop: frozen-mutability  ·  scope: {scope}",
                "Deterministic signal: no mutable container fields on frozen "
                "models.",
                "",
                "RESULT: PASS — frozen models carry only immutable fields.",
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
            f"Loop: frozen-mutability  ·  scope: {scope}  ·  "
            f"{len(hits)} field(s)" + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
