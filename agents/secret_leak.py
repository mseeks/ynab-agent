"""secret-leak loop (Many Hands Engineering, read-only).

Net-new, and timely: the project now wires AgentMail and (soon) YNAB keys, which
must always come from the environment, never the repo (a committed secret
is the worst-case slip — it survives in history even after deletion). This loop
is the regression tripwire.

Signal: a pure scan for a *secret-named* assignment bound to a *literal string*
long enough to be a real credential — ``api_key = "…"`` / ``token: "…"`` /
``SECRET = "…"`` with a 16+ char value. Lines that read from the environment
(``os.environ`` / ``getenv``) are excluded, since that is the correct pattern.

Action: a read-only agent Reads each hit and sorts it — *Secret* (a genuine
hardcoded credential: rotate it and move it to the environment) / *Not a secret*
(a placeholder, an example, a test fixture, a non-secret constant the name
over-matched) / *Judgment-heavy* — quoting the line (cite-or-omit), and NEVER
echoing the secret value in full.

Usage:
    python -m agents.secret_leak [scope]   # a path; default: src
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

# A credential-ish identifier next to a binding.
_SECRET_NAME = re.compile(
    r"\b(api[_-]?key|secret|token|password|passwd|"
    r"access[_-]?token|client[_-]?secret|bearer)\b",
    re.IGNORECASE,
)
# A string literal long enough to be a real key (16+ chars).
_LITERAL = re.compile(r"""[:=]\s*["']([^"']{16,})["']""")
# Reading a secret from the environment is the correct pattern, not a leak.
_FROM_ENV = re.compile(r"\b(os\.environ|getenv|environ\[)")
# Obvious non-secrets a credential name may sit next to.
_PLACEHOLDER = re.compile(
    r"your[_-]|example|placeholder|changeme|dummy|fake|xxxx|<[^>]+>|\.\.\.",
    re.IGNORECASE,
)


def scan_secret_leaks(target: Path) -> list[Hit]:
    """Find secret-named assignments bound to a long literal (the signal)."""
    hits: list[Hit] = []
    for path in iter_python_files(target):
        text = path.read_text()
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not _SECRET_NAME.search(raw):
                continue
            if _FROM_ENV.search(raw):
                continue
            match = _LITERAL.search(raw)
            if match is None:
                continue
            if _PLACEHOLDER.search(match.group(1)):
                continue
            snippet = raw.strip()[:SNIPPET_MAX]
            hits.append(Hit(rel, lineno, "literal-secret", snippet))
    return sorted(hits, key=lambda h: (h.path, h.line))


_SYSTEM_PROMPT = """\
You are the secret-leak loop for a Python project. You are READ-ONLY: you map
possible hardcoded credentials; you never edit, and you NEVER echo a secret
value in full (quote the variable + a masked prefix only). A human acts on it.

Credentials must come from the environment, never the repo — a committed secret
survives in git history even after deletion. You are given a deterministic list
of secret-named assignments bound to a 16+ char string literal (lines that read
from the environment are already excluded). Read each in context and sort it:

- SECRET — a genuine hardcoded credential (an API key, token, or password). Say
  "rotate it and read from the environment instead." Quote the variable and a
  masked value (first 4 chars + …), never the whole thing.
- NOT A SECRET — a false positive: a placeholder/example, a test fixture value,
  a public identifier, or a non-secret constant the name over-matched. Say why.
- JUDGMENT-HEAVY — plausibly sensitive but unclear (e.g. a sample token in a
  doc). State the tradeoff.

HARD RULES:
- Cite or omit, but mask the value. Never reproduce a full secret.
- A real key in a *test* is still a real key — flag it.
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} candidate secret(s) across "
        f"{files} file(s) under `{scope}`"
        + (f" (showing the first {MAX_HITS})" if truncated else "")
        + ":",
        "",
    ]
    for i, hit in enumerate(shown, start=1):
        lines.append(f"[{i}] {hit.path}:{hit.line}  |  {hit.text}")
    lines += [
        "",
        "Read each in context, then produce the three-bucket map "
        "(mask every value).",
    ]
    return "\n".join(lines)


async def main() -> int:
    """Run the secret-leak loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: secret-leak  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_secret_leaks(target)
    if not hits:
        report(
            [
                f"Loop: secret-leak  ·  scope: {scope}",
                "Deterministic signal: no secret-named literal assignments.",
                "",
                "RESULT: PASS — no hardcoded credentials in scope.",
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
            f"Loop: secret-leak  ·  scope: {scope}  ·  "
            f"{len(hits)} candidate(s)"
            + ("  (truncated)" if truncated else ""),
            f"Agent run: {result.run}  ·  cost: ${result.cost_usd:.4f}",
            "",
            result.summary or "(no map produced)",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
