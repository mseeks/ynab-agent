"""Shared harness for the Many Hands Engineering loops.

A Python port of the Revisionist ``agents/lib.ts``. Each loop is just a
*signal/action prompt* + a *tool allowlist* + a *report*. Everything identical
lives here:

* auth — Claude subscription OAuth, never an API key;
* the standard, locked-down ``query()`` invocation (``dontAsk`` + an allowlist,
  hermetic settings) — the permission model is entirely SDK-native, so anything
  not on the allowlist is denied without a callback;
* the always-on, test-style report.

The first loops are *read-only mappers*: they never write, so the harness omits
the write-loop machinery (suite-green, revert-on-red) that a future autonomous
loop will add. Keeping Stage 1 narrow is deliberate (replicate before
broadening).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent  # the agents/ package dir
APP_ROOT = HERE.parent  # the ynab-agent project root

# Read-only mapper loops get exactly these tools. `dontAsk` denies the rest.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]

# Per-run defaults shared by the loops.
DEFAULT_MODEL = "sonnet"  # the sweet spot for read-only mappers
DEFAULT_MAX_BUDGET_USD = 1.0

# Signal defaults shared by the regex-sweep mapper loops.
SNIPPET_MAX = 160  # max chars of a source line shown to the model
MAX_HITS = 60  # max markers handed to one run (keeps the prompt bounded)

_ENV_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$")


# ── The deterministic signal: a regex sweep over Python files ───────────────
@dataclass(frozen=True)
class Hit:
    """One deterministic marker hit (the unit of a mapper loop's signal).

    Attributes:
        path: Path relative to the project root.
        line: 1-based line number.
        kind: The marker kind (e.g. ``"any"``, ``"TODO"``).
        text: The source line, stripped and length-capped.
    """

    path: str
    line: int
    kind: str
    text: str


def iter_python_files(target: Path) -> list[Path]:
    """Return the ``.py`` files under ``target`` (or ``target`` itself).

    The ``agents/`` package is always excluded: a loop's own module contains its
    marker patterns as string literals, which would otherwise self-flag.

    Args:
        target: A file or directory to scan.

    Returns:
        The matching files, ordered by path.
    """
    if target.is_file():
        return [target] if target.suffix == ".py" else []
    files: list[Path] = []
    for path in sorted(target.rglob("*.py")):
        parts = set(path.parts)
        if "agents" in parts or "__pycache__" in parts:
            continue
        files.append(path)
    return files


def sweep(
    target: Path, markers: tuple[tuple[re.Pattern[str], str], ...]
) -> list[Hit]:
    """Sweep ``target`` for regex ``markers``. Pure and deterministic.

    One hit is recorded per (line, marker kind); a line matching two kinds
    yields two hits. This is the shared outside-reference signal every mapper
    loop is grounded on.

    Args:
        target: A file or directory to scan.
        markers: ``(compiled_pattern, kind)`` pairs to search each line for.

    Returns:
        Hits ordered by path then line.
    """
    hits: list[Hit] = []
    for path in iter_python_files(target):
        try:
            rel = str(path.relative_to(APP_ROOT))
        except ValueError:
            rel = str(path)
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
            for pattern, kind in markers:
                if pattern.search(raw):
                    snippet = raw.strip()[:SNIPPET_MAX]
                    hits.append(
                        Hit(path=rel, line=lineno, kind=kind, text=snippet)
                    )
    return hits


@dataclass(frozen=True)
class LoopResult:
    """The outcome of one agent pass.

    Attributes:
        run: ``"completed"`` or ``"ended early — <reason>"``.
        summary: The agent's final text (the loop's map). Empty if none.
        cost_usd: Reported run cost, or ``0.0`` if the SDK gave none.
    """

    run: str
    summary: str
    cost_usd: float


def arg_scope() -> str | None:
    """Return the first positional CLI argument (a path scope), if any."""
    positionals = [a for a in sys.argv[1:] if not a.startswith("-")]
    return positionals[0] if positionals else None


# ── Auth: Claude subscription OAuth, never an API key ───────────────────────
def _load_env_file(env_file: Path) -> bool:
    """Load ``KEY=VALUE`` pairs from ``env_file`` into ``os.environ``.

    Existing values are never clobbered.

    Args:
        env_file: Path to a dotenv-style file.

    Returns:
        ``True`` if the file existed and was read, else ``False``.
    """
    if not env_file.exists():
        return False
    for line in env_file.read_text().splitlines():
        match = _ENV_LINE.match(line)
        if match and os.environ.get(match.group(1)) is None:
            os.environ[match.group(1)] = match.group(2).strip().strip("\"'")
    return True


def resolve_oauth_token() -> str:
    """Resolve the Claude Code OAuth token, exiting with guidance if absent.

    Resolution order: an existing ``CLAUDE_CODE_OAUTH_TOKEN`` in the
    environment, then the local ``agents/.env``, then a sibling project's
    ``agents/.env`` (a git worktree shares one token across projects).

    Returns:
        The resolved token.

    Raises:
        SystemExit: If no token can be found.
    """
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        _load_env_file(HERE / ".env")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        # Fallback: reuse a sibling project's token (worktrees share one).
        projects_dir = APP_ROOT.parent
        for sibling in sorted(projects_dir.glob("*/agents/.env")):
            if _load_env_file(sibling) and os.environ.get(
                "CLAUDE_CODE_OAUTH_TOKEN"
            ):
                break
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        print(
            "RESULT: CANNOT RUN — CLAUDE_CODE_OAUTH_TOKEN is not set.\n"
            "  Run `claude setup-token`, then put the token in agents/.env\n"
            "  (see agents/.env.example). In a worktree, the token is\n"
            "  inherited from a sibling project's agents/.env automatically.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return token


def _loop_env(token: str) -> dict[str, str]:
    """Return a child env that forces subscription OAuth and strips API keys."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


# ── The agent run: the standard locked-down query() ─────────────────────────
async def run_loop(
    *,
    system_prompt: str,
    prompt: str,
    allowed_tools: list[str] = READ_ONLY_TOOLS,
    model: str = DEFAULT_MODEL,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
) -> LoopResult:
    """Run one locked-down, read-only Claude Agent SDK pass.

    The permission model is SDK-native: ``permission_mode="dontAsk"`` denies
    any tool not in ``allowed_tools`` (no callback), ``setting_sources=[]``
    keeps the run hermetic (no workspace settings), and ``.env`` reads are
    explicitly denied so secrets never enter context.

    Args:
        system_prompt: The loop's role/instructions.
        prompt: The task, including the deterministic signal to verify.
        allowed_tools: The tool allowlist. Defaults to read-only tools.
        model: Model alias or id. Defaults to ``"sonnet"``.
        max_budget_usd: Hard per-run spend cap.

    Returns:
        A :class:`LoopResult` with the run status, the agent's map, and cost.
    """
    token = resolve_oauth_token()
    # Lazy import: keep pure signal code importable without the loops extra.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        ToolUseBlock,
        query,
    )

    options = ClaudeAgentOptions(
        cwd=APP_ROOT,
        model=model,
        max_turns=250,
        max_budget_usd=max_budget_usd,
        permission_mode="dontAsk",
        setting_sources=[],
        env=_loop_env(token),
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=["Read(.env)"],
    )

    run = "completed"
    summary = ""
    cost = 0.0
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    # Stream tool-use markers as live progress; the narrative
                    # text is captured from the final ResultMessage instead.
                    if isinstance(block, ToolUseBlock):
                        sys.stdout.write(f"  · {block.name}\n")
                if message.error is not None:
                    run = f"ended early — {message.error}"
            elif isinstance(message, ResultMessage):
                if message.result:
                    summary = message.result
                if message.total_cost_usd is not None:
                    cost = message.total_cost_usd
                if message.is_error:
                    detail = ", ".join(message.errors or []) or message.subtype
                    run = f"ended early — {detail}"
    except Exception as err:  # surface any SDK failure as a run status
        run = f"ended early — {err}"
    return LoopResult(run=run, summary=summary, cost_usd=cost)


# ── Report (always emitted, test-style) ─────────────────────────────────────
def report(lines: list[str]) -> None:
    """Print a test-style report block."""
    print("\n" + "═" * 64)
    for line in lines:
        print(line)
