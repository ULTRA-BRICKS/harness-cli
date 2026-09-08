"""Claude Code OAuth usage JSON parser — real windows only, no token-count fallback."""

from __future__ import annotations

from harness_cli.providers.claude import parse_oauth_usage, window_label


def test_parse_five_hour_and_seven_day():
    payload = {
        "five_hour": {"utilization": 42.0, "resets_at": "2026-09-08T15:12:00Z"},
        "seven_day": {"utilization": 81.0, "resets_at": "2026-09-10T16:00:00Z"},
    }
    windows = parse_oauth_usage(payload)
    names = [w.name for w in windows]
    assert names == ["Session", "Weekly"]
    assert windows[0].used_percent == 42.0
    assert windows[0].window_seconds == 5 * 3600
    assert windows[1].used_percent == 81.0
    assert windows[1].window_seconds == 7 * 86400
    assert windows[0].resets_at is not None
    assert windows[1].resets_at is not None


def test_parse_seven_day_sonnet_and_skip_null_opus():
    payload = {
        "five_hour": {"utilization": 10.0, "resets_at": "2026-09-08T15:00:00Z"},
        "seven_day": {"utilization": 20.0, "resets_at": "2026-09-10T16:00:00Z"},
        "seven_day_sonnet": {"utilization": 33.0, "resets_at": "2026-09-10T16:00:00Z"},
        "seven_day_opus": None,
    }
    windows = parse_oauth_usage(payload)
    names = [w.name for w in windows]
    assert names == ["Session", "Weekly", "Weekly·Sonnet"]
    assert all(w.name != "Weekly·Opus" for w in windows)


def test_unknown_seven_day_fable_becomes_weekly_fable():
    assert window_label("seven_day_fable") == "Weekly·Fable"
    payload = {
        "seven_day_fable": {"utilization": 96.0, "resets_at": "2026-09-10T16:00:00Z"},
    }
    windows = parse_oauth_usage(payload)
    assert len(windows) == 1
    assert windows[0].name == "Weekly·Fable"
    assert windows[0].used_percent == 96.0
    assert windows[0].window_seconds == 7 * 86400


def test_skips_non_dict_and_missing_utilization():
    payload = {
        "five_hour": {"resets_at": "2026-09-08T15:00:00Z"},
        "seven_day": "not-a-window",
        "extra": 1,
    }
    assert parse_oauth_usage(payload) == []


def test_parse_skips_non_finite_utilization():
    payload = {
        "five_hour": {"utilization": float("inf"), "resets_at": "2026-09-08T15:00:00Z"},
        "seven_day": {"utilization": float("nan"), "resets_at": "2026-09-10T16:00:00Z"},
    }
    assert parse_oauth_usage(payload) == []
