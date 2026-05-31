"""doc-coherence loop (Many Hands Engineering, read-only).

Signal: a pure scan of the Markdown docs for *mechanical* drift — relative links
to files that no longer exist, backtick-quoted file paths that are gone, and
``make <target>`` mentions whose Makefile target was removed.

Action: a locked-down, read-only agent verifies each mechanical hit in context
(a broken-looking ref may be intentional history) AND finds the *semantic* drift
the scan cannot — claims about code, architecture, or behavior that no longer
match reality. It emits a strict three-bucket map — *Drift confirmed* (with
WHAT/WHY/ACTION) / *Not drift* (verified, intentional, or historical) /
*Judgment-heavy* — cite-or-omit, with a hard rule against stylistic nitpicks.
The loop writes nothing; the doc voice stays yours.

Mirrors Revisionist loop #5 (same shape, a doc-specific signal).

Usage:
    python -m agents.doc_coherence [scope]   # a .md file or dir; default: .
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
    report,
    run_loop,
)

if TYPE_CHECKING:
    from pathlib import Path

_SKIP_DIRS = {
    ".venv",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
}
_PATH_EXTS = {
    "py",
    "toml",
    "md",
    "txt",
    "lock",
    "cfg",
    "ini",
    "sh",
    "json",
    "yaml",
    "yml",
}

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_BACKTICK = re.compile(r"`([^`]+)`")
_MAKE_TARGET = re.compile(r"^([A-Za-z][\w-]*):")


def _iter_markdown_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix == ".md" else []
    files: list[Path] = []
    for path in sorted(target.rglob("*.md")):
        if _SKIP_DIRS & set(path.parts):
            continue
        files.append(path)
    return files


def _make_targets() -> set[str]:
    makefile = APP_ROOT / "Makefile"
    if not makefile.exists():
        return set()
    targets: set[str] = set()
    for line in makefile.read_text().splitlines():
        match = _MAKE_TARGET.match(line)
        if match:
            targets.add(match.group(1))
    return targets


def _looks_like_path(text: str) -> bool:
    if text.startswith("-") or not re.fullmatch(r"[\w./-]+", text):
        return False
    return "/" in text or text.rsplit(".", 1)[-1] in _PATH_EXTS


def _exists(rel: str, base: Path) -> bool:
    return (APP_ROOT / rel).exists() or (base / rel).exists()


def scan_doc_drift(target: Path) -> list[Hit]:
    """Scan Markdown under ``target`` for mechanical doc drift (the signal)."""
    hits: list[Hit] = []
    make_targets = _make_targets()
    for doc in _iter_markdown_files(target):
        try:
            rel = str(doc.relative_to(APP_ROOT))
        except ValueError:
            rel = str(doc)
        base = doc.parent
        for lineno, raw in enumerate(doc.read_text().splitlines(), start=1):
            snippet = raw.strip()[:SNIPPET_MAX]
            for link in _LINK.finditer(raw):
                tgt = link.group(1).split()[0].split("#")[0].split("?")[0]
                if not tgt or tgt.startswith(("http", "#", "mailto:")):
                    continue
                if not _exists(tgt, base):
                    hits.append(Hit(rel, lineno, "broken-link", snippet))
            for span in _BACKTICK.finditer(raw):
                inner = span.group(1).strip()
                if inner.startswith("make "):
                    parts = inner.split()
                    target_name = parts[1] if len(parts) > 1 else ""
                    if target_name and target_name not in make_targets:
                        hits.append(Hit(rel, lineno, "missing-make", snippet))
                elif _looks_like_path(inner) and not _exists(inner, base):
                    hits.append(Hit(rel, lineno, "missing-path", snippet))
    return hits


_SYSTEM_PROMPT = """\
You are the doc-coherence loop for a Python project. You are READ-ONLY: you map
documentation drift; you never edit. A human acts on your map.

You are given a deterministic list of MECHANICAL hits (broken links, missing
file paths, removed make targets). For each, Read the doc line and the
referenced target to confirm it. THEN, beyond the mechanical hits, Read the docs
against the actual code (Read/Grep/Glob) and find SEMANTIC drift: claims about
code, architecture, commands, or behavior that no longer match reality.

Classify every issue into exactly one bucket:

- DRIFT CONFIRMED — a real mismatch between the doc and current reality. State
  WHAT is wrong, WHY it misleads, and the concrete ACTION (the corrected fact).
  Quote the doc line with file:line.
- NOT DRIFT — a mechanical hit that is actually fine (an intentional/historical
  reference, an anchor, a path that does exist). State the specific reason.
- JUDGMENT-HEAVY — a possible drift whose resolution needs an authoring call.

HARD RULES:
- Cite or omit. Quote only doc lines you actually observed. Never invent a
  reference or a claim.
- No stylistic preferences, no hypothetical drift, no rewriting the doc's voice.
  Only factual mismatches with the code as it is now.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} mechanical doc-drift hit(s) under "
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
        "Verify each, then also hunt for semantic drift vs the code.",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the doc-coherence loop. Returns a process exit code."""
    scope = arg_scope() or "."
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [f"Loop: doc-coherence  ·  scope: {scope}", f"Not found: {target}"]
        )
        return 1

    hits = scan_doc_drift(target)
    truncated = len(hits) > MAX_HITS
    result = await run_loop(
        system_prompt=_SYSTEM_PROMPT,
        prompt=_format_signal(scope, hits, truncated),
    )
    report(
        [
            f"Loop: doc-coherence  ·  scope: {scope}  ·  "
            f"{len(hits)} mechanical hit(s)"
            + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
