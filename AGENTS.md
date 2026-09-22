# AGENTS.md — harness-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`harness limits` reports **real rate-limit proximity** (percent used, reset
countdown, pace) from each provider's usage API. `harness stats` reports
per-product-ticket review/fix iteration counts, follow-up chains, and
best-effort local-transcript token usage — a DIFFERENT, product-tracking
concern from `limits`, sharing only this repo's fail-closed discipline and
its existing transcript-search machinery. It is a peer to `task-cli` /
`tg-cli` / `review-cli` / `dev-cli` (each a standalone repo under
`alex-mextner`) and shells out (read-only) to task-cli's `task` binary and to
`gh` for `stats`'s own data.

See `README.md` for data sources and fail-closed rules.

## Hard rules

- **No fabricated percents.** `used_percent` comes from a usage API or the card
  is fail-closed. Never compute a percent from transcript token/message counts.
- **Never print OAuth tokens or Authorization headers.** Error strings name the
  HTTP status (`unauthorized (HTTP 401)`), not the secret.
- **Stdlib-only at import time.** `harness_cli/cli.py`'s `build_parser()` and
  module import must succeed with zero third-party imports. Heavy/network/subprocess
  work is imported lazily inside `_run_limits` / `_run_search` / `_run_tasks` /
  `_run_agents` / `_run_nudge` / `_run_stats`.
- **Fail closed.** Missing creds, 401/403/429, or no usage endpoint → a card
  with a reason, empty windows, no bar, no fake `% used`.
- Transcript modules stay; do not delete them. They must not feed `limits`.
- **`stats` never fabricates a review count, follow-up, or token total.** A missing
  `task`/`gh` binary, an unresolved ticket, zero matching PRs, or no local transcript
  match each produce their own `*_unavailable_reason` (or `review_iterations_reason`)
  string — never a silent `0`/`[]`/`None` standing in for "the lookup itself failed".
  `stats` never imports task-cli as a Python module; it only shells out to the
  already-installed `task`/`gh` binaries and parses their `--json` stdout, exactly
  like `nudge.py`/`omp_acp.py` already shell out to `omp acp`.

## Style

- Conventional commits.
- English-only code, comments, and docs.
- No dead code, no underscore-prefixed unused params.
- Keep `cli.py` thin (argparse + dispatch); behavior lives in the sibling modules.
- Add a test with every behavior change. TDD red-first is the house style.
- Function length target ~50 lines, hard ceiling ~80 — decompose before it grows past that.

## Tests

- `python -m pytest -q` — hermetic unit suite. Every test that touches the
  filesystem uses `tmp_path` and monkeypatches `$HOME` (and Claude/OpenCode env
  vars). Never the real developer `$HOME`. HTTP is mocked at `urllib.request.urlopen`.
- `ruff check .` — lint.
