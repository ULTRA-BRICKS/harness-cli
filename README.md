# harness-cli

`harness limits` shows **real rate-limit proximity** for agent-harness accounts on
this machine: per-account cards with Session (5h) / Weekly (7d) bracketed bars, `% used`,
reset countdown, and a pace line when the current rate would exhaust the window
before reset.

Percents come from each provider's usage API. They are **never** guessed from
local transcript token counts (that is a different problem, which
[ccusage](https://ccusage.com) already covers).

```
Claude Code  invntrm@gmail.com  Max 20x
  Session (5h)      [██████████░░░░░░░░░░░░░░]  42% used  resets in 3h 12m
  Weekly (7d)       [███████████████████░░░░░]  81% used  resets in 2d 4h
  Weekly·Fable (7d) [███████████████████████░]  96% used  resets in 2d 4h
  At this pace you'll run out in 2h 29m
  Updated 12s ago
```

Missing credentials or a missing usage endpoint fail closed (`missing credentials
— not guessed from tokens` / `no usage endpoint — not guessed from tokens`) instead
of inventing a number.

## Commands

```
harness limits --current [--json]
harness limits --all [--window-hours N] [--json]
harness search QUERY [--days N]
harness task new --title TITLE [--body BODY]
harness task list [--all] [--json]
harness task done ID
harness agents [--json] [--all]
harness agents web [--host H] [--port N] [--no-open]
```

- `--all` — every known account/provider on this machine (Claude Code slots,
  Codex, z.ai, x.ai, kimi).
- `--current` — the same proximity renderer for the current Claude Code account
  (`$CLAUDE_CONFIG_DIR` or `~/.claude`).
- `--json` — the proximity model (`cards` + `windows`), not transcript token dumps.
- `--window-hours` — accepted for compatibility and still validated (finite,
  non-negative, not overflowing). It does **not** drive a fake percent for `--all`.
- `search` finds user messages in Claude Code and Oh My Pi *parent* session transcripts
  (`QUERY` is a case-insensitive substring; `--days` defaults to 3, `0` disables).
- `task` is the local process-ticket tracker for agent/session work; it writes JSON under
  `$HARNESS_TASK_DIR` (else XDG/state) and never files a GitHub issue.
- `agents` lists OMP and Claude Code parent + nested sessions, pending tg-ctl questions,
  and open harness tasks. Default age window is 24 hours (`--all` disables it and includes
  done tasks). Live vs parked is an mtime heuristic (under 10 minutes is live), never a
  process-liveness API. `agents web` serves a local page at http://127.0.0.1:7888
  (`--port 0` binds ephemeral; `--no-open` skips the browser) with links to the review
  dashboard (7878), spec-web (7920), rig config-web (8787), rig evolve (8797), and 3d web
  (8733).

## Data sources (real % only)

| Provider | Source | Fail-closed when |
| --- | --- | --- |
| Claude Code | `GET https://api.anthropic.com/api/oauth/usage` with the local OAuth access token. `limits[]` → Session / Weekly / Weekly·Fable. | No `.credentials.json` / keychain / `CLAUDE_CODE_OAUTH_TOKEN`; HTTP 401/403/429 |
| Codex | `GET https://chatgpt.com/backend-api/wham/usage` from `~/.codex/auth.json` | Missing tokens; HTTP error; payload without `used_percent` |
| z.ai / GLM | `GET https://api.z.ai/api/monitor/usage/quota/limit` (Authorization = raw API key, **no** Bearer prefix) from OpenCode `auth.json` | Missing key; HTTP error |
| kimi | `GET https://api.kimi.com/coding/v1/usages` Bearer coding-plan key; `% = 100*(limit-remaining)/limit` | Missing key; HTTP error |
| x.ai | No public percent usage API | Always `no usage endpoint — not guessed from tokens` |

Claude Code slots scanned: `~/.claude` plus `~/.claude-accounts/account-{0,1,2}`.
On macOS the Keychain token is preferred when `~/.claude/.credentials.json` is stale.


## Architecture

- `harness_cli/cli.py` — thin argparse dispatch; network work is lazy-imported
  inside `_run_limits` so `harness --help` stays stdlib-only.
- `harness_cli/limits_model.py` / `limits_render.py` / `limits_collect.py` —
  cards, TUI, collector.
- `harness_cli/providers/` — `claude.py`, `codex.py`, `zai.py`, `xai.py`, `kimi.py`.
- `harness_cli/search.py` — parent-session user-message search (CC + OMP).
- `harness_cli/tasks.py` — local process-ticket store (not GitHub).
- `harness_cli/agents.py` — landscape collectors (OMP/CC agents, tg-ctl questions, tasks).
- `harness_cli/agents_web.py` — local live landscape web view (ThreadingHTTPServer + SSE).
- Stdlib only (no rich). Unicode bars (`█` / `░`). ANSI green below 90%, red at
  ≥90%. Honors `NO_COLOR` / `FORCE_COLOR` / isatty.


## Tests

```
python -m pytest -q
ruff check .
```
