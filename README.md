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

## Install

Pick one — each ends with `harness` on your PATH:

```bash
# 1. uv — recommended; macOS, Linux and Windows (incl. Cygwin / Git Bash)
uv tool install hyper-harness

# 2. pipx
pipx install hyper-harness

# 3. from a clone — to hack on it; `git pull` updates the installed tool
git clone https://git.hyperide.ai/ultrabricks/harness-cli && cd harness-cli && uv tool install --editable .
```

- **Unreleased `main`:** `uv tool install git+https://git.hyperide.ai/ultrabricks/harness-cli` (or the same with `pipx install`).
- **Update:** `uv tool upgrade hyper-harness` · `pipx upgrade hyper-harness` · `git pull`.
- **Windows:** use uv (option 1). If a command dies with `UnicodeEncodeError` in mintty, run `setx PYTHONUTF8 1` once. More: [rig-cli → Windows](https://git.hyperide.ai/ultrabricks/rig-cli#windows-cygwin-git-bash-powershell).

> **On PyPI as `hyper-harness`** (the command is still `harness`). `harness-cli` on PyPI is an unrelated project — `pipx install harness-cli` would install that. The canonical repo is
> **git.hyperide.ai/ultrabricks/harness-cli**; `github.com/alex-mextner/harness-cli` is a frozen, archived mirror.

## Commands

```
harness limits --current [--json]
harness limits --all [--window-hours N] [--json]
harness search QUERY [--days N]
harness tasks new --title TITLE [--body BODY]
harness tasks list [--all] [--json]
harness tasks done ID
harness tasks import PATH
harness agents [--json] [--all] [--live]
harness agents web [--host H] [--port N] [--no-open]
harness nudge AGENT_ID MESSAGE [--json] [--timeout-ms N]
harness stats TICKET [--repo owner/name] [--json]
harness stats --all [--repo owner/name] [--json]
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
- `tasks` is the local process-ticket tracker for agent/session work; it writes JSON under
  `$HARNESS_TASK_DIR` (else XDG/state) and never files a GitHub issue. States are
  `open`/`done`/`blocked`/`duplicate`/`dropped`; `list` defaults to `open` only, `--all`
  shows every state. `tasks import PATH` idempotently upserts tickets from a JSON array
  keyed on `(source, external_id)`, printing created/updated/total counts.
- `agents` lists OMP and Claude Code parent + nested sessions, pending tg-ctl questions,
  and open harness tasks. Default age window is 24 hours (`--all` disables it and includes
  done, blocked, duplicate, and dropped tasks). Live vs parked is an mtime heuristic (under
  10 minutes is live), never a process-liveness API. `agents web` serves a local page at
  http://127.0.0.1:7888
  (`--port 0` binds ephemeral; `--no-open` skips the browser) with links to the review
  dashboard (7878), spec-web (7920), rig config-web (8787), rig evolve (8797), and 3d web
  (8733).
- `--live` (on `agents`) — restrict to `status == "live"` rows and additionally
  resolve `cwd`/`session_id` for kinds whose handler supports it (currently `omp`);
  every row also carries a cheap `nudge_supported` bool regardless of `--live`.
- `nudge` sends a message to one live agent by its `agents` row `id`
  (`harness:kind:path`). Universal adapter API: per-kind protocol knowledge lives
  behind `harness_cli/nudge.py`'s registry (`harness_cli/omp_acp.py` is the only real
  implementation today — a faithful, one-shot port of tg-ctl's ACP JSON-RPC client).
  `--json` emits `{"ok": true}` or `{"ok": false, "error": "...", "reason":
  "unsupported"|"failed"|"not-found"}`; exit codes 0/1/2/3 mirror ok/failed/
  unsupported/not-found so a scripted caller can distinguish "never gonna work for
  this kind" from "transient failure, maybe retry". IPC is a subprocess+JSON contract
  over argv/stdout, not a daemon: harness-cli is stdlib-only, one-shot argparse, with
  zero daemon infra, and this is fire-and-forget (at most once per silence episode),
  not a chat loop.
- `stats` reports, for one PRODUCT ticket (not a `tasks` local process ticket), review/fix
  iteration counts, its follow-up chain, and best-effort local-transcript token usage.
  `TICKET` is `owner/repo#N` or a bare `N` (needs `--repo`, or a git origin remote in the
  current directory that resolves to one). `--all` reports every CLOSED ticket in the repo
  plus an aggregate summary (review-iteration buckets, count with a follow-up, count with
  token data) instead of one ticket — a calibration dataset, not a story-point suggestion
  (that's `task classify`'s job). Every stat is independently fail-closed with its own
  `*_reason` field: a missing `task`/`gh` binary, an unresolved ticket, no matching PR, or
  no local transcript match never collapses into a fake `0`/empty result — see "Data
  sources" below.

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

### Data sources (`stats` command — never a fake number, either)

| Stat | Source | Fail-closed when |
| --- | --- | --- |
| Follow-up chain | `task read <id> --repo <owner/repo> --json` (sibling task-cli, read-only); greps the returned `links` dict for `"Followed up by #<id>"` keys | `task` not on PATH; ticket id doesn't resolve; ticket genuinely has no follow-up link (reported as `followups: []` with no reason — distinct from a lookup failure, which always carries a reason) |
| Review iterations | `gh pr list --search "<id> in:body" --repo <owner/repo> --json number` then `gh pr view <n> --json reviews,commits`; counted as the number of `CHANGES_REQUESTED` reviews (documented proxy — see `stats.count_review_iterations`'s docstring for its known blind spot) | `gh` not on PATH; no PR found referencing the ticket |
| Token usage | Reuses `harness_cli.search`'s existing local Claude Code/OMP transcript scan for the ticket id or its title, then sums real `harness_cli.transcripts` `SessionUsage` token fields across matches — never estimated | No local transcript mentions the ticket |



## Architecture

- `harness_cli/cli.py` — thin argparse dispatch; network work is lazy-imported
  inside `_run_limits` so `harness --help` stays stdlib-only.
- `harness_cli/limits_model.py` / `limits_render.py` / `limits_collect.py` —
  cards, TUI, collector.
- `harness_cli/providers/` — `claude.py`, `codex.py`, `zai.py`, `xai.py`, `kimi.py`.
- `harness_cli/search.py` — parent-session user-message search (CC + OMP).
- `harness_cli/tasks.py` — local process-ticket store (not GitHub).
- `harness_cli/agents.py` — landscape collectors (OMP/CC agents, tg-ctl questions, tasks).
- `harness_cli/nudge.py` — per-kind (harness) nudge registry; the ONLY place that
  decides which harness supports a live nudge and how to resolve its live target.
- `harness_cli/omp_acp.py` — omp ACP JSON-RPC client (NDJSON framing over `omp acp`
  stdio); the one real `KIND_HANDLERS` implementation.
- `harness_cli/agents_web.py` — local live landscape web view (ThreadingHTTPServer + SSE).
- `harness_cli/stats.py` — `stats` command: review/fix iteration counts (`gh`),
  follow-up chains (`task`), and best-effort transcript token usage (reuses
  `harness_cli/search.py` + `harness_cli/transcripts.py`); subprocess work is
  lazy-imported inside `_run_stats` so `harness --help` stays stdlib-only.
- Stdlib only (no rich). Unicode bars (`█` / `░`). ANSI green below 90%, red at
  ≥90%. Honors `NO_COLOR` / `FORCE_COLOR` / isatty.


## Tests

```
python -m pytest -q
ruff check .
```