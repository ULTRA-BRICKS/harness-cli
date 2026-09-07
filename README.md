# harness-cli

`harness` reports **observed** token/message/api-error signals from local agent harness
session transcripts — starting with Claude Code. It exists so other tools in the ecosystem
(`review-cli` picking reviewer-board seats, `tg-cli`, `pm-cli`) can query something before
launching more concurrent work, instead of finding out about a session-limit cooldown only
after a 429-style error already happened.

## What this is NOT (and the real next step it's deferring)

There is no **documented, stable, official** API for "seconds until your rate limit resets" or
"% of your 5-hour/7-day window consumed" for a Claude Code (subscription-plan) session. But a
real, undocumented one DOES exist and was found during research: Claude Code's own desktop/CLI
statusline queries `https://api.anthropic.com/api/oauth/usage` (Bearer token from the local
OAuth credentials, plus an `anthropic-beta: oauth-2025-04-20` header) and gets back
`five_hour.utilization` / `seven_day.utilization` (0-100%) and each window's `resets_at`. That
IS a real predictive limit-proximity signal, sitting on disk-adjacent credentials today.

**Deliberately not built against it in this MVP**, for three concrete reasons found during
research, not vibes: (1) it's reverse-engineered and unversioned — Anthropic could change or
remove it with no notice; (2) it has its own separate rate-limiting bucket and has been reported
to return persistent 429s under some conditions
(`anthropics/claude-code#31021`); (3) it needs its own credential-reading + HTTP layer, which is
a second data source with a different trust/failure model than "read local JSONL" — worth doing
as a deliberate follow-up, not folded silently into the read-only-transcript MVP. This is the
concrete next step, not a hypothetical one — see "Explicitly NOT built yet" below.

The `anthropic-ratelimit-*` HTTP response headers exist on raw API calls too, but Claude Code
doesn't surface them to the user or to any local file either.

So today, `harness` reports what's actually already on local disk, with zero network calls:

- **Token/message counts** — the exact `usage` fields (`input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`) Claude Code itself writes per
  assistant turn into its transcript files.
- **Observed API-error events** — transcript lines Claude Code itself marked
  `isApiErrorMessage: true`, categorized from real observed text (`session_limit`,
  `weekly_limit`, `model_limit`, `rate_limit`, `overloaded`, `safety_flag`, `auth`, `other`).
  When the message includes one (session/weekly-limit messages do, verbatim: `"resets 7:30pm
  (Europe/Belgrade)"`), the raw reset-time hint is extracted and surfaced too — not parsed into
  an absolute timestamp, since the message never states the date and doing so would fabricate
  precision that isn't there.

This is mostly a **reactive** signal today (how often are we hitting limits, and when did we
last hit one, with a real wall-clock hint when the message provides one) — not the fully
**predictive** live-utilization-percentage signal the OAuth endpoint could someday provide.

## Prior art considered

- **[ccusage](https://ccusage.com)** (npm, actively maintained, MIT) is the closest real match:
  it reads the exact same `~/.claude/projects/**/*.jsonl` files, computes cost/token reports
  (daily/weekly/session/5-hour-block), and has a live "active block" view. It does NOT query the
  OAuth usage endpoint — it computes cost from local token counts, a different problem from "how
  close am I to a 429". `harness-cli` takes the same "read the local JSONL" approach as its
  primary data source (validated directly against real transcripts on this machine).
- **[Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)**
  (Python, PyPI, actively maintained) goes further than ccusage: it does burn-rate
  extrapolation ("at this token rate, this session's 5h window runs out at HH:MM") from local
  JSONL alone, and a tracked issue
  (`Maciek-roboblog/Claude-Code-Usage-Monitor#202`) proposes wiring in the same OAuth usage
  endpoint below for authoritative cross-device state — confirming that endpoint is real and
  known in this exact tool space, not something this research invented.
- The OAuth usage endpoint itself (`api.anthropic.com/api/oauth/usage`) is documented
  community-side by several statusline projects (e.g. `ohugonnot/claude-code-statusline`) and
  discussed directly on Anthropic's own `anthropics/claude-code` GitHub issue tracker (issue
  `#31021`, about that endpoint's own 429s) — see "What this is NOT" above for why it's not used
  here yet.

## Commands

```
harness limits --current [--json]
harness limits --all [--window-hours N] [--json]
```

- `--current` resolves and reads **this session's own transcript** (via `$CLAUDE_CONFIG_DIR` +
  `$CLAUDE_CODE_SESSION_ID`; falls back to "most recently modified transcript" when those don't
  resolve directly, e.g. inside a subagent — the fallback reason is always stated explicitly).
- `--all` scans every known account (`~/.claude` plus every `~/.claude-accounts/account-*`) and
  every project under it, aggregating token/message/error counts per account.
  `--window-hours` (default 5, matching Claude Code's rolling session window) skips transcript
  files whose mtime is older than the window, as a cheap pre-filter before parsing.

## Known limitation: the mtime pre-filter is a heuristic in BOTH directions

`--window-hours` first filters files by filesystem mtime, for performance (some accounts on
this machine have 1GB+ of transcripts).

- **Fresh mtime, stale content** — found against real data during development: a transcript's
  file mtime can be recent while its actual last *message* (its own `timestamp` field) is
  months old (something outside `harness-cli` touches these files - spotlight/backup/sync -
  without appending content). **Fixed**: after parsing, `cli._scan_all_sessions` re-checks each
  session's real `last_timestamp` and drops anything still outside the window. A session with no
  timestamp at all is kept rather than guessed away. Covered by
  `tests/test_cli.py::test_limits_all_excludes_stale_content_despite_fresh_mtime`.
- **Stale mtime, fresh content** — the inverse, found during code review, NOT fixed: if a
  transcript's file mtime is older than the window cutoff, it's skipped before ever being
  parsed, even if (hypothetically) its content were somehow fresher than its mtime — e.g. a
  restore/copy that rewrites bytes but preserves an old mtime. This is a real gap of the
  pre-filter, accepted for this MVP because closing it would mean parsing every file on every
  run regardless of mtime, defeating the whole point of the pre-filter on multi-GB accounts.
  Under Claude Code's own normal write pattern (append-only, mtime always advances with
  content) this can't happen; it's only a risk from external tooling manipulating the files.

## Known limitation: directory-level symlink swap is a TOCTOU gap (accepted, not fixed)

Every FILE this tool opens is confined against symlinks and non-regular-file tricks atomically
(open-then-`fstat`, see `transcripts._open_transcript_atomically`), and every DIRECTORY root
(`~/.claude`, each `account-*`, each `projects/`) is checked with `is_symlink()` before being
walked (`discovery._safe_is_real_dir`). What is NOT fully closed: a directory could, in
principle, be *replaced* by a symlink in the gap between that check and the `os.walk()` call
that follows it (`os.walk` follows its explicitly-given root, and file-level `O_NOFOLLOW`
protects only the final path component, not an ancestor directory swapped out from under it).
Found during code review; **deliberately not fixed** for this MVP: closing it properly needs
`openat`/directory-file-descriptor-relative traversal (Python's `os.walk` has no simple portable
way to do this), which is real added complexity for a race that requires the SAME local user
account to be concurrently and adversarially manipulating their own `~/.claude*` directories
while `harness` happens to be scanning them — there is no privilege boundary being crossed (the
attacker already owns every file `harness` could read), so the practical impact is confined to
"this tool reports the wrong data to the same user who could just read the files directly
anyway," not a security boundary violation. Reassess if this tool is ever run against
another user's data or over a shared/network filesystem.

## Known limitation: Windows symlink race, and unbounded single-line memory use (accepted, not fixed)

Two more findings from code review, deliberately accepted rather than fixed for this MVP:

- **Windows symlink race.** `transcripts._open_transcript_atomically`'s `O_NOFOLLOW` protection
  is POSIX-only (`getattr(os, "O_NOFOLLOW", 0)` silently becomes a no-op where the flag doesn't
  exist). On Windows, a `*.jsonl` path could in principle be swapped for a symlink between
  discovery's `is_symlink()` check and the open, and get read anyway. Accepted because this
  ecosystem's tooling (see `AGENTS.md`'s "VS Code Extension Debugging" and every other repo's
  conventions) is macOS-only in practice; revisit if this tool is ever run on Windows.
- **Unbounded single-line memory use.** Each `.jsonl` line is read and `json.loads`-parsed with
  no size cap. A pathological single line (a multi-GB unterminated line, or a huge string field)
  would force a large allocation and could raise `MemoryError`, uncaught. Accepted because the
  input is the SAME local user's own Claude Code transcripts, not untrusted network input, and
  real transcript lines are at most a few KB; a size cap is a reasonable follow-up if this tool
  is ever pointed at transcripts it doesn't already trust the size of.

## Architecture

- `harness_cli/discovery.py` — locates account config dirs and transcript files.
- `harness_cli/transcripts.py` — parses one `.jsonl` transcript into a `SessionUsage`.
- `harness_cli/model.py` — data model + error-text classification.
- `harness_cli/report.py` — text/JSON rendering.
- `harness_cli/cli.py` — thin argparse dispatch.

Adding a second harness (opencode/omp/codex) means adding a sibling module that produces the
same `SessionUsage` shape from that harness's own local log format, and wiring it into `cli.py`
— `discovery`/`model`/`report` are already harness-agnostic in shape (only `transcripts.py` is
Claude-Code-specific today).

## Explicitly NOT built yet (MVP scope)

- **The OAuth usage endpoint integration** (real predictive `five_hour.utilization` /
  `seven_day.utilization` / `resets_at`) — the concrete next step, deliberately deferred; see
  "What this is NOT" above for the three reasons.
- No `harness watch` live-updating view.
- No pluggable multi-harness registry — only Claude Code has a source module; the interface
  is anticipated (`parse_transcript` -> `SessionUsage`) but not yet abstracted into a plugin
  system since there's only one real implementation to generalize from.
- No historical trend/retention (e.g. "errors per day over the last month") — `--all` scans
  what's currently on disk within the window, nothing is persisted between runs.
- **`--window-hours` filters at the SESSION level, not the per-message level.** A session
  touched once inside the window contributes its FULL lifetime token/message totals to the
  report, not just what happened inside the window (`render_all` says so explicitly in its own
  output). Per-record windowing (summing only the usage lines whose own timestamp falls inside
  the window) would give a tighter number but adds real complexity - deferred rather than
  guessed at silently.
- Windows symlink-race hardening and a per-line size cap — see the "Known limitation" section
  above for why each is accepted rather than fixed here.
- Not published to PyPI, not installed system-wide; not pushed to a GitHub remote (see below).

## Status

This is a local git repo with an initial commit only. It has **not** been pushed to GitHub and
no remote repo has been created — that's a real decision (public/private, org placement) left
for explicit sign-off, same as the reports-repo question from earlier work.

## Tests

```
python -m pytest -q
ruff check .
```
