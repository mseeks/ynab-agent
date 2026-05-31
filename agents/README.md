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
| `debug-cruft` | regex sweep for `print(` / `breakpoint()` / `pdb` / pytest `skip`/`xfail` | three-bucket map: *Delete now* / *Intentional* / *Stale skip — re-enable* |
| `doc-coherence` | Markdown scan for broken links, missing file paths, removed `make` targets (+ agent finds semantic drift vs code) | three-bucket map: *Drift confirmed* / *Not drift* / *Judgment-heavy* |
| `duplicated-constant` | scan for the same numeric literal re-typed on a rule line (comparison / `timedelta` / `Field` bound / `Money`) at 2+ sites | three-bucket map: *Centralise now* / *Legitimately repeated* / *Judgment-heavy* |
| `dead-code` | AST scan for top-level defs/classes whose name is never referenced (as a word) in `src/` or `tests/` | three-bucket map: *Delete now* / *Reachable* (framework false positive) / *Judgment-heavy* |

File discovery (`lib.iter_python_files`) and the locked-down agent pass
(`lib.run_loop`) are shared; the regex-sweep loops (`type-debt`, `comment-debt`,
`debug-cruft`) add only a marker set plus a system prompt, while the rest bring a
purpose-built scan (a Markdown ref-check, a literal-dedup, an AST reference
count) — the framework's "replicate the template" shape (Stage 2).

## Running

```bash
make sync                          # installs the loops extra (claude-agent-sdk)
make loop-type-debt                # scope defaults to src/
make loop-comment-debt SCOPE=src/ynab_agent/domain
# or directly:
uv run python -m agents.type_debt [scope]
uv run python -m agents.comment_debt [scope]
uv run python -m agents.debug_cruft [scope]
uv run python -m agents.doc_coherence [scope]
uv run python -m agents.duplicated_constant [scope]
uv run python -m agents.dead_code [scope]
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
