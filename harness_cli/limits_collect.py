"""Tiny collector: one card list from every provider on this machine.

Accessed via: cli._run_limits (lazy import). Never reads transcripts for a
percent — each provider fail-closes when creds or the usage endpoint are missing.
"""

from __future__ import annotations

from pathlib import Path

from harness_cli.limits_model import LimitCard
from harness_cli.providers.claude import collect_claude
from harness_cli.providers.codex import collect_codex
from harness_cli.providers.kimi import collect_kimi
from harness_cli.providers.xai import collect_xai
from harness_cli.providers.zai import collect_zai


def collect_all(home: Path | None = None) -> list[LimitCard]:
    home = home or Path.home()
    cards: list[LimitCard] = []
    cards.extend(collect_claude(home))
    cards.append(collect_codex(home))
    cards.append(collect_zai(home))
    cards.append(collect_xai(home))
    cards.append(collect_kimi(home))
    return cards


def collect_current(home: Path | None = None) -> list[LimitCard]:
    home = home or Path.home()
    return collect_claude(home, current_only=True)
