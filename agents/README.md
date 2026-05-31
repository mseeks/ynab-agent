# Loops

Many Hands Engineering loops for the YNAB agent. A loop is a small, reversible
cycle — *signal → action → verification → report* — run as part of the
development cycle. Each is a deterministic signal (computed in plain Python)
plus a locked-down, **read-only** Claude Agent SDK pass that verifies each hit
in context and emits a strict, **cite-or-omit** map. Loops propose; you address.

We are at framework Stage 1: one loop, closed end-to-end, as the template. More
loops of the *same shape* come next — we resist broadening any single one.

## Loops

| Loop | Signal | Output |
|---|---|---|
| `type-debt` | regex sweep for `Any`, `cast(`, `# type: ignore` / `# mypy:` / `# pyright:` | three-bucket map: *Tightenable now* / *Legitimately needed* / *Judgment-heavy* |
| `comment-debt` | regex sweep for `TODO` / `FIXME` / `HACK` / `XXX` | three-bucket map: *Action now* / *Legitimately kept* / *Judgment-heavy* |

The deterministic sweep (`lib.sweep`) and file discovery are shared; each loop
is just its marker set plus a system prompt — the framework's "replicate the
template" shape (Stage 2).

## Running

```bash
make sync                          # installs the loops extra (claude-agent-sdk)
make loop-type-debt                # scope defaults to src/
make loop-comment-debt SCOPE=src/ynab_agent/domain
# or directly:
uv run python -m agents.type_debt [scope]
uv run python -m agents.comment_debt [scope]
```

## Auth & safety

- **Auth:** Claude subscription OAuth (`CLAUDE_CODE_OAUTH_TOKEN`), never an API
  key. Run `claude setup-token` once and put the token in `agents/.env` (see
  `.env.example`); a worktree reuses a sibling project's token automatically.
- **Read-only:** the agent gets only `Read`/`Grep`/`Glob`. `permission_mode`
  is `dontAsk`, so anything off the allowlist is denied — it cannot write, run
  shell, or read `.env`. Settings are hermetic (`setting_sources=[]`).
- **Budget:** a hard `max_budget_usd` cap per run (default $1).
- **Idempotency:** there is no findings cache; each run is fresh against the
  live tree. "Addressed" findings are the ones you fixed — the next run no
  longer surfaces them.
