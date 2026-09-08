"""Renderer tests for the rate-limit proximity TUI.

These pin the user-visible contract: bars, "% used", reset countdown, ANSI red at
90/100 (not 89), pace only when exhaustion beats reset, 5h/7d duration tags on
bracketed tracks, and no fake percent on a fail-closed card. Production renderer
only — no reimplemented bars in the test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harness_cli.limits_model import LimitCard, LimitWindow
from harness_cli.limits_render import render_cards

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _window(
    name: str,
    used_percent: float,
    *,
    remaining: timedelta,
    window_seconds: float,
) -> LimitWindow:
    return LimitWindow(
        name=name,
        used_percent=used_percent,
        resets_at=NOW + remaining,
        window_seconds=window_seconds,
    )


def _live_card(*windows: LimitWindow, fetched_at: datetime | None = None) -> LimitCard:
    return LimitCard(
        provider="Claude Code",
        account="invntrm@gmail.com",
        plan="Max 20x",
        windows=list(windows),
        fetched_at=fetched_at or (NOW - timedelta(seconds=12)),
        status="ok",
    )


def test_render_includes_bar_percent_and_reset_countdown():
    card = _live_card(
        _window("Session", 42.0, remaining=timedelta(hours=3, minutes=12), window_seconds=5 * 3600)
    )
    out = render_cards([card], now=NOW, color=False)

    assert "█" in out
    assert "░" in out
    assert "42% used" in out
    assert "resets in 3h 12m" in out
    assert "Claude Code" in out
    assert "invntrm@gmail.com" in out
    assert "Max 20x" in out
    assert "Updated 12s ago" in out


def test_red_ansi_at_90_and_100_not_at_89():
    red = "\x1b[31m"
    win_90 = _window("Session", 90.0, remaining=timedelta(hours=1), window_seconds=5 * 3600)
    win_100 = _window("Weekly", 100.0, remaining=timedelta(hours=1), window_seconds=7 * 86400)
    win_89 = _window("Session", 89.0, remaining=timedelta(hours=1), window_seconds=5 * 3600)

    out_90 = render_cards([_live_card(win_90)], now=NOW, color=True)
    out_100 = render_cards([_live_card(win_100)], now=NOW, color=True)
    out_89 = render_cards([_live_card(win_89)], now=NOW, color=True)

    assert red in out_90
    assert "90% used" in out_90
    assert red in out_100
    assert "100% used" in out_100
    assert red not in out_89
    assert "89% used" in out_89


def test_pace_present_when_exhaustion_beats_reset():
    # 90% used, 2h remaining of a 5h window → elapsed 3h, rate 30%/h, 10% left → 20m.
    card = _live_card(
        _window("Session", 90.0, remaining=timedelta(hours=2), window_seconds=5 * 3600)
    )
    out = render_cards([card], now=NOW, color=False)
    assert "At this pace you'll run out in" in out


def test_pace_absent_when_window_will_reset_first():
    # 10% used, 4h remaining of a 5h window → elapsed 1h, rate 10%/h, 90% left → 9h > 4h.
    card = _live_card(
        _window("Session", 10.0, remaining=timedelta(hours=4), window_seconds=5 * 3600)
    )
    out = render_cards([card], now=NOW, color=False)
    assert "At this pace you'll run out in" not in out


def test_missing_creds_card_has_no_fake_percent():
    card = LimitCard(
        provider="Claude Code",
        account="mextner@gmail.com",
        plan=None,
        windows=[],
        fetched_at=NOW,
        status="missing_creds",
        reason="missing credentials — not guessed from tokens",
    )
    out = render_cards([card], now=NOW, color=False)

    assert "% used" not in out
    assert "█" not in out
    assert "%" not in out
    assert "missing credentials" in out
    assert "not guessed from tokens" in out
    assert "mextner@gmail.com" in out


def test_session_5h_line_has_duration_tag_and_bracketed_bar():
    card = _live_card(
        _window("Session", 42.0, remaining=timedelta(hours=3, minutes=12), window_seconds=5 * 3600)
    )
    out = render_cards([card], now=NOW, color=False)
    line = next(ln for ln in out.splitlines() if "Session" in ln and "%" in ln)
    assert "Session (5h)" in line
    assert "[" in line and "]" in line
    track = line[line.index("[") + 1 : line.index("]")]
    assert "█" in track
    assert "░" in track
    assert "42% used" in line
    assert "resets in 3h 12m" in line


def test_weekly_7d_line_has_duration_tag_and_bracketed_bar():
    card = _live_card(
        _window("Weekly", 81.0, remaining=timedelta(days=2, hours=4), window_seconds=7 * 86400)
    )
    out = render_cards([card], now=NOW, color=False)
    line = next(ln for ln in out.splitlines() if "Weekly" in ln)
    assert "Weekly (7d)" in line
    assert "[" in line and "]" in line
    track = line[line.index("[") + 1 : line.index("]")]
    assert "█" in track
    assert "░" in track


def test_fail_closed_card_has_no_bracket_bar_or_percent():
    card = LimitCard(
        provider="Claude Code",
        account="mextner@gmail.com",
        plan=None,
        windows=[],
        fetched_at=NOW,
        status="missing_creds",
        reason="missing credentials — not guessed from tokens",
    )
    out = render_cards([card], now=NOW, color=False)
    assert "[" not in out
    assert "█" not in out
    assert "%" not in out
    assert "missing credentials" in out


def test_missing_window_seconds_omits_duration_tag():
    card = _live_card(
        LimitWindow(
            name="Session",
            used_percent=42.0,
            resets_at=NOW + timedelta(hours=3, minutes=12),
            window_seconds=None,
        )
    )
    out = render_cards([card], now=NOW, color=False)
    assert "Session (5h)" not in out
    assert "Session" in out
    assert "42% used" in out


def test_window_bars_column_align_with_longest_label():
    card = _live_card(
        _window("Session", 42.0, remaining=timedelta(hours=3, minutes=12), window_seconds=5 * 3600),
        _window(
            "Weekly·Fable", 96.0, remaining=timedelta(days=2, hours=4), window_seconds=7 * 86400
        ),
    )
    out = render_cards([card], now=NOW, color=False)
    lines = [ln for ln in out.splitlines() if "[" in ln]
    assert len(lines) == 2
    assert "Session (5h)" in lines[0]
    assert "Weekly·Fable (7d)" in lines[1]
    assert lines[0].index("[") == lines[1].index("[")

def test_window_bars_column_align_with_long_scoped_name():
    card = _live_card(
        _window("Session", 42.0, remaining=timedelta(hours=3, minutes=12), window_seconds=5 * 3600),
        _window(
            "Weekly·Opus 4.6", 96.0, remaining=timedelta(days=2, hours=4), window_seconds=7 * 86400
        ),
    )
    out = render_cards([card], now=NOW, color=False)
    lines = [ln for ln in out.splitlines() if "[" in ln]
    assert len(lines) == 2
    assert "Session (5h)" in lines[0]
    assert "Weekly·Opus 4.6 (7d)" in lines[1]
    assert lines[0].index("[") == lines[1].index("[")
