#!/usr/bin/env python3
"""Temporal determinism kernel — the lexical workflow-boundary check.

Fails when a known-nondeterministic call appears *lexically inside* a
``@workflow.defn`` class. This is the high-precision / low-recall seed of the
determinism review: it inspects only code written directly in a workflow class
body — not transitive helpers or imported functions (that is the "brain's" job,
a later ring) — so a *blocking* CI gate built on it has near-zero false
positives, which is the property a gate must have or humans disable it.

Scope: every ``@workflow.defn`` class under the given paths. Activities
(``@activity.defn``), client setup, type modules, and module-level helpers are
out of scope by construction — nondeterminism is legal there.

Usage:
    python check_determinism.py [--json] PATH [PATH ...]

Exit 0 if clean, 1 if any hazard is found. ``--json`` prints the findings as a
JSON array (file, line, col, rule, hint, source) — the structured seam a later
analysis ring consumes instead of re-parsing.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# --- the banned table -------------------------------------------------------
# Each entry maps a resolved callee / attribute to its sanctioned replacement.
# Kept deliberately tight: every entry must be a near-certain hazard inside a
# workflow body, so the gate stays precise enough to block on.

# Exact-match callee paths (resolved through the file's imports).
BANNED_CALLS: dict[str, str] = {
    "datetime.datetime.now": "use workflow.now()",
    "datetime.datetime.utcnow": "use workflow.now()",
    "datetime.datetime.today": "use workflow.now()",
    "datetime.date.today": "use workflow.now().date()",
    "time.time": "use workflow.now()",
    "time.time_ns": "use workflow.now()",
    "time.monotonic": "use workflow.now()",
    "time.monotonic_ns": "use workflow.now()",
    "time.perf_counter": "use workflow.now()",
    "time.perf_counter_ns": "use workflow.now()",
    "time.process_time": "use workflow.now()",
    "time.sleep": "use workflow.sleep()",
    "uuid.uuid1": "use workflow.uuid4()",
    "uuid.uuid3": "use workflow.uuid4()",
    "uuid.uuid4": "use workflow.uuid4()",
    "uuid.uuid5": "use workflow.uuid4()",
    "os.getenv": "read env in an activity and pass it in",
    "os.urandom": "use workflow.random()",
    "asyncio.sleep": "use workflow.sleep()",
    "socket.socket": "do network I/O in an activity",
}

# Any call whose resolved root module is one of these (module-wide impurity).
# Only modules with *no* deterministic surface worth keeping belong here.
BANNED_CALL_MODULES: dict[str, str] = {
    "random": "use workflow.random()",
    "threading": "workflows are single-threaded; use workflow APIs",
    "requests": "do network I/O in an activity",
    "httpx": "do network I/O in an activity",
    "subprocess": "run external processes in an activity",
}

# Attribute *accesses* (need not be calls), e.g. os.environ["X"].
BANNED_ATTRS: dict[str, str] = {
    "os.environ": "read env in an activity and pass it in",
}

# Bare builtin names — flagged only when not shadowed by an import binding.
BANNED_BUILTINS: dict[str, str] = {
    "open": "do file I/O in an activity",
    "input": "workflows cannot block on input",
}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    col: int
    rule: str  # the resolved hazard, e.g. "datetime.datetime.now"
    hint: str
    source: str


def _import_map(tree: ast.Module) -> dict[str, str]:
    """Map each bound name to the dotted module/symbol path it refers to."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    bindings[root] = root
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:  # relative import — out of scope
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                bindings[bound] = f"{node.module}.{alias.name}"
    return bindings


def _dotted(node: ast.AST) -> str | None:
    """The literal dotted path of a Name/Attribute chain, or None."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _resolve(node: ast.AST, imports: dict[str, str]) -> str | None:
    """Dotted path with the root name substituted via the import map.

    Returns None when the root is not an imported symbol — local variables and
    attributes on instances (e.g. a stored Random) are out of scope, which is
    what keeps the check precise.
    """
    dotted = _dotted(node)
    if dotted is None:
        return None
    root, _, rest = dotted.partition(".")
    if root not in imports:
        return None
    base = imports[root]
    return f"{base}.{rest}" if rest else base


def _is_workflow_class(node: ast.ClassDef, imports: dict[str, str]) -> bool:
    """True if the class carries an ``@workflow.defn`` (or aliased) decorator."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        resolved = _resolve(target, imports) or _dotted(target)
        if resolved and resolved.endswith("workflow.defn"):
            return True
    return False


def scan_file(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    imports = _import_map(tree)
    lines = text.splitlines()
    findings: list[Finding] = []
    seen: set[tuple[int, int, str]] = set()

    def record(node: ast.expr, rule: str, hint: str) -> None:
        key = (node.lineno, node.col_offset, rule)
        if key in seen:
            return
        seen.add(key)
        src = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
        findings.append(Finding(str(path), node.lineno, node.col_offset, rule, hint, src))

    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if not _is_workflow_class(cls, imports):
            continue
        for node in ast.walk(cls):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id in BANNED_BUILTINS
                    and func.id not in imports
                ):
                    record(node, func.id, BANNED_BUILTINS[func.id])
                    continue
                resolved = _resolve(func, imports)
                if resolved is None:
                    continue
                if resolved in BANNED_CALLS:
                    record(node, resolved, BANNED_CALLS[resolved])
                elif resolved.split(".")[0] in BANNED_CALL_MODULES:
                    record(node, resolved, BANNED_CALL_MODULES[resolved.split(".")[0]])
            elif isinstance(node, ast.Attribute):
                resolved = _resolve(node, imports)
                if resolved in BANNED_ATTRS:
                    record(node, resolved, BANNED_ATTRS[resolved])

    return findings


def _iter_py(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        p = Path(root)
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            files.extend(
                f
                for f in sorted(p.rglob("*.py"))
                if "__pycache__" not in f.parts
            )
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="files or directories to scan")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    for path in _iter_py(args.paths):
        findings.extend(scan_file(path))

    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
        return 1 if findings else 0

    if not findings:
        print("No determinism hazards in workflow code. ✓")
        return 0

    for f in findings:
        print(f"{f.file}:{f.line}:{f.col}  {f.rule}  ->  {f.hint}")
        print(f"    {f.source}")
    noun = "hazard" if len(findings) == 1 else "hazards"
    print(f"\n{len(findings)} determinism {noun} in workflow code.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
