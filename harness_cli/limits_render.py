"""Stdlib proximity TUI: bars, % used, reset countdown, pace, ANSI color.

Accessed via: `harness limits` after limits_collect returns cards.
Assumptions: used_percent comes from a real usage API. Fail-closed cards must
not grow a bar or a percent. Color honors NO_COLOR / FORCE_COLOR / isatty.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import TextIO

from harness_cli.limits_model import LimitCard, LimitWindow

BAR_WIDTH = 24
RED = "\x1b[31m"
GREEN = "\x1b[32m"
RESET = "\x1b[0m"
RED_AT = 90.0


def color_enabled(stream: TextIO | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream if stream is not None else sys.stdout
    return bool(hasattr(stream, "isatty") and stream.isatty())


def format_duration(seconds: float) -> str:
    """Compact countdown: `12s`, `3h 12m`, `2d 4h`."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def format_ago(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s ago"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def render_bar(percent: float, width: int = BAR_WIDTH) -> str:
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round(pct / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def pace_run_out_seconds(window: LimitWindow, now: datetime) -> float | None:
    """Seconds until 100% at the implied rate, or None if reset wins / unknown."""
    if window.used_percent is None or window.resets_at is None or window.window_seconds is None:
        return None
    used = float(window.used_percent)
    if used <= 0 or used >= 100:
        return None
    remaining = (window.resets_at - now).total_seconds()
    if remaining <= 0:
        return None
    elapsed = float(window.window_seconds) - remaining
    if elapsed <= 0:
        return None
    rate = used / elapsed
    if rate <= 0:
        return None
    tte = (100.0 - used) / rate
    if tte < remaining:
        return tte
    return None


def card_pace_seconds(card: LimitCard, now: datetime) -> float | None:
    soonest: float | None = None
    for window in card.windows:
        tte = pace_run_out_seconds(window, now)
        if tte is None:
            continue
        if soonest is None or tte < soonest:
            soonest = tte
    return soonest


def _paint(text: str, color: bool, percent: float | None) -> str:
    if not color or percent is None:
        return text
    code = RED if percent >= RED_AT else GREEN
    return f"{code}{text}{RESET}"


def _now(now: datetime | None) -> datetime:
    if now is not None:
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def render_window_line(window: LimitWindow, now: datetime, color: bool) -> str:
    label = f"  {window.name:<15}"
    if window.used_percent is None:
        return f"{label}unknown"
    pct = float(window.used_percent)
    if not math.isfinite(pct):
        return f"{label}unknown"
    pct = max(0.0, min(100.0, pct))
    bar = _paint(render_bar(pct), color, pct)
    shown = f"{int(round(pct))}% used"
    reset = ""
    if window.resets_at is not None:
        remaining = (window.resets_at - now).total_seconds()
        reset = f"  resets in {format_duration(remaining)}"
    return f"{label}{bar}  {shown}{reset}"


def render_card(card: LimitCard, now: datetime, color: bool) -> str:
    header = f"{card.provider}  {card.account}"
    if card.plan:
        header = f"{header}  {card.plan}"
    lines = [header]
    if card.status != "ok" or not card.windows:
        reason = card.reason or "unavailable — not guessed from tokens"
        lines.append(f"  {reason}")
    else:
        for window in card.windows:
            lines.append(render_window_line(window, now, color))
        pace = card_pace_seconds(card, now)
        if pace is not None:
            lines.append(f"  At this pace you'll run out in {format_duration(pace)}")
    if card.fetched_at is not None:
        ago = (now - card.fetched_at).total_seconds()
        lines.append(f"  Updated {format_ago(ago)}")
    return "\n".join(lines)


def render_cards(
    cards: list[LimitCard],
    *,
    now: datetime | None = None,
    color: bool = False,
) -> str:
    current = _now(now)
    if not cards:
        return "No rate-limit accounts found on this machine."
    return "\n\n".join(render_card(card, current, color) for card in cards)


def cards_to_json(cards: list[LimitCard]) -> str:
    return json.dumps({"cards": [c.to_dict() for c in cards]}, indent=2)
