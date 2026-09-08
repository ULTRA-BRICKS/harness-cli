# AGENTS.md — harness-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`harness` reports OBSERVED token/message/api-error signals read from local agent harness
session transcripts, starting with Claude Code. It is a peer to `task-cli` / `tg-cli` /
`review-cli` / `dev-cli` (each a standalone repo under `alex-mextner`), not folded into
`rig-cli` (provisioning/setup, not operational) or `review-cli` (the reviewer-board consumer
this tool exists to serve, not the right owner of a cross-cutting limits signal).

See `README.md` for the full "what this is / is not" explanation, prior-art notes (ccusage and
others), and the known mtime-pre-filter limitation.

## Hard rules

- **No fabricated numbers.** There is no documented, stable API for Claude Code
  rate-limit-remaining or time-to-reset. Never invent one. Report only what is actually present
  in a transcript line: real `usage` fields, real `isApiErrorMessage: true` events, real
  timestamps/counts. A missing field is `0`/`unknown`, never estimated.
- **Stdlib-only at import time.** `harness_cli/cli.py`'s `build_parser()` and module import
  must succeed with zero third-party imports, so `harness --help` / `harness --version` never
  need a dependency. Heavy work (transcript scanning / search / task store) is imported lazily inside `_run_limits` / `_run_search` / `_run_task`.
- **Every fallback states its reason.** `discovery.find_current_transcript` returns a `how`
  string explaining which resolution path was actually used (direct session-id match vs.
  most-recently-modified fallback) — never a silent guess.
- **Harness-agnostic shape, Claude-Code-specific implementation.** `model.SessionUsage` and
  `report.py` are written generically; only `transcripts.py` knows Claude Code's on-disk JSONL
  format. A second harness gets its own sibling parser module producing the same
  `SessionUsage`, not a fork of `report.py`.

## Style

- Conventional commits.
- English-only code, comments, and docs.
- No dead code, no underscore-prefixed unused params.
- Keep `cli.py` thin (argparse + dispatch); behavior lives in the sibling modules.
- Add a test with every behavior change. TDD red-first is the house style.
- Function length target ~50 lines, hard ceiling ~80 — decompose before it grows past that.

## Tests

- `python -m pytest -q` — hermetic unit suite. Every test that touches "the filesystem" uses
  `tmp_path` and monkeypatches `$HOME` / the Claude Code env vars
  (`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`) — never the real
  developer `$HOME` or real `~/.claude*` transcripts.
- `ruff check .` — lint.

## Not yet done

See README.md's "Explicitly NOT built yet" section — no `harness watch`, no multi-harness
plugin registry (only Claude Code has a real source module today), no persisted history across
runs, no PyPI publish, no GitHub remote.
