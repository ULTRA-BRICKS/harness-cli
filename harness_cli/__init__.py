"""harness-cli: observed usage/limit signals for local agent harnesses.

MVP scope (see README.md "What this does NOT do" for the honest ceiling):
reads Claude Code's own local session transcripts (the same `~/.claude/projects/**/*.jsonl`
/ `~/.claude-accounts/account-*/projects/**/*.jsonl` files Claude Code itself writes) and
reports OBSERVED token/message counts and OBSERVED api-error events (rate-limit / session-limit
/ overloaded / auth messages Claude Code already recorded). It does not predict time-to-reset or
percent-of-window-remaining — no stable, documented API exposes that today.
"""

__version__ = "0.1.0"
