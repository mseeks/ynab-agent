"""model-seam loop (Many Hands Engineering, read-only).

Net-new and project-specific — a regression guard for a production outage. The
agentic middle talks to a local Ollama/Gemma, where Ollama bug #15288 routes
Gemma's text into the response ``reasoning`` field and leaves ``content`` empty.
Under Pydantic AI's default *tool* output that empties the assistant turn, so a
retry echoes back ``content: null`` with no ``tool_calls`` and Ollama answers
``400 invalid message content type: <nil>`` — failing categorization and reply
handling. The fix funnels every agent through one seam,
``agentic/model.py:run_structured``, which forces ``NativeOutput`` +
``reasoning_effort: "none"`` on the production path.

The invariant this loop guards: **every Pydantic AI agent invocation goes
through that seam.** A direct ``agent.run(...)``, an ad-hoc ``Agent(...)`` built
outside the ``agentic`` package, or a private ``_AGENT`` imported across modules
all bypass it and silently reopen the 400 hazard.

Signal: a pure, location-aware sweep. It flags (a) a ``.run(`` call inside
the ``agentic`` package but outside ``model.py`` (the one sanctioned
``agent.run`` lives *inside* ``run_structured``); (b) an ``Agent(`` built
anywhere in ``src`` *outside* the ``agentic`` package; and (c) any
``import`` that names a private ``_AGENT``. A clean tree returns PASS.

Action: a read-only agent Reads each hit and sorts it — *Bypass* (route it
through ``run_structured``; quote the line) / *Safe* (a false positive: the
call is ``run_structured`` itself, a comment, a ``TYPE_CHECKING`` annotation,
a non-Pydantic-AI ``Agent``, or a test scaffold) / *Judgment-heavy* —
cite-or-omit. The loop writes nothing; you fix what it confirms.

Usage:
    python -m agents.model_seam [scope]   # a path; default: src
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

# The seam itself: the only module where calling ``agent.run`` is sanctioned.
_AGENTIC_PKG = "ynab_agent/agentic/"
_SEAM_MODULE = "ynab_agent/agentic/model.py"

# A direct agent invocation (``_AGENT.run(`` / ``agent.run(``). The only
# sanctioned ``agent.run`` lives in ``run_structured`` (no ``.run(`` here).
_DIRECT_RUN = re.compile(r"\.run\(")
# A Pydantic AI ``Agent(...)`` construction. ``Agent[...]`` (a type
# annotation) and ``AgentMail(`` / ``ClaudeAgentOptions(`` do not match.
_AGENT_CTOR = re.compile(r"\bAgent\(")
# Importing a module-private agent object out of its module (a cross-module
# bypass). The definition line ``_AGENT: Agent[...] = Agent(...)`` has no
# ``import``, so it never matches.
_AGENT_IMPORT = re.compile(r"\bimport\b.*\b_AGENT\b")


def _norm(rel: str) -> str:
    """Forward-slashed relative path for portable substring checks."""
    return rel.replace("\\", "/")


def scan_model_seam(target: Path) -> list[Hit]:
    """Find agent invocations that bypass ``run_structured`` (the signal).

    Three location-aware markers, each a genuine bypass of the model seam:

    * ``direct-run`` — a ``.run(`` inside the ``agentic`` package but outside
      ``model.py`` (the lone sanctioned ``agent.run`` is in ``run_structured``).
    * ``external-agent`` — an ``Agent(`` construction outside the ``agentic``
      package (an ad-hoc agent that never sees the seam).
    * ``agent-import`` — an ``import`` naming a private ``_AGENT`` (pulling an
      agent object out of its module to call it raw elsewhere).

    Args:
        target: A file or directory to scan.

    Returns:
        Hits ordered by path then line.
    """
    hits: list[Hit] = []
    for path in iter_python_files(target):
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        norm = _norm(rel)
        in_agentic = _AGENTIC_PKG in norm
        is_seam = norm.endswith(_SEAM_MODULE)
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
            snippet = raw.strip()[:SNIPPET_MAX]
            if in_agentic and not is_seam and _DIRECT_RUN.search(raw):
                hits.append(Hit(rel, lineno, "direct-run", snippet))
            if not in_agentic and _AGENT_CTOR.search(raw):
                hits.append(Hit(rel, lineno, "external-agent", snippet))
            if _AGENT_IMPORT.search(raw):
                hits.append(Hit(rel, lineno, "agent-import", snippet))
    return sorted(hits, key=lambda h: (h.path, h.line, h.kind))


_SYSTEM_PROMPT = """\
You are the model-seam loop for a Temporal + Pydantic AI project. You are
READ-ONLY: you map agent invocations that bypass the model seam; you never edit.
A human fixes what you confirm.

Every Pydantic AI agent in this project MUST run through one seam,
`agentic/model.py:run_structured(...)`, which forces NativeOutput +
`reasoning_effort: "none"` on the production path. It fixes Ollama bug
#15288 (Gemma routes text into `reasoning`, leaves `content` empty; the default
tool output then retries with `content: null` and Ollama returns
`400 invalid message content type: <nil>`, breaking categorization/replies). A
direct `agent.run(...)`, an ad-hoc `Agent(...)` built outside the `agentic`
package, or a private `_AGENT` imported across modules all reopen that hazard.

You are given a deterministic list of candidate bypasses. Read each in context
and sort it into exactly one bucket:

- BYPASS — a real bypass: a `.run(` on an agent outside `run_structured`, an
  `Agent(...)` built outside `agentic/`, or an imported `_AGENT`. Say "route it
  through `run_structured`" (extend the seam if it is a brand-new agent) and
  quote the line.
- SAFE — a false positive: the call IS `run_structured`/inside `model.py`, a
  comment/docstring, a `TYPE_CHECKING`-only `Agent[...]` annotation (not a
  call), a non-Pydantic-AI object that merely has a `.run(` method, or a test
  scaffold. Name which.
- JUDGMENT-HEAVY — a deliberate new pattern (e.g. a new agent entrypoint the
  seam should grow to cover). State the tradeoff.

HARD RULES:
- Cite or omit. Quote the line you Read, or drop the hit. Never invent.
- The fix is real: prefer naming "route through run_structured".
- Be terse. This is a map, not an essay.

End with a one-line RESULT summarizing counts per bucket."""


def _format_signal(scope: str, hits: list[Hit], truncated: bool) -> str:
    """Build the task prompt embedding the deterministic signal."""
    shown = hits[:MAX_HITS]
    files = len({h.path for h in hits})
    lines = [
        f"DETERMINISTIC SIGNAL — {len(hits)} candidate bypass(es) across "
        f"{files} file(s) under `{scope}`"
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
    """Run the model-seam loop. Returns a process exit code."""
    scope = arg_scope() or "src"
    target = APP_ROOT / scope
    if not target.exists():
        report(
            [
                f"Loop: model-seam  ·  scope: {scope}",
                f"Not found: {target}",
            ]
        )
        return 1

    hits = scan_model_seam(target)
    if not hits:
        report(
            [
                f"Loop: model-seam  ·  scope: {scope}",
                "Deterministic signal: no agent invocation bypasses "
                "run_structured.",
                "",
                "RESULT: PASS — every agent run goes through the model seam.",
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
            f"Loop: model-seam  ·  scope: {scope}  ·  "
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
