"""harness-cli: real rate-limit proximity for local agent harness accounts.

`harness limits` queries each provider's usage API (Claude Code OAuth usage,
Codex wham/usage, z.ai quota/limit, kimi coding usages) and renders percent-used
bars, reset countdowns, and pace. Percents are never guessed from local
transcript token counts. Transcript modules remain for other commands.
"""

__version__ = "0.3.0"
