from __future__ import annotations

from harness_cli.model import ApiErrorEvent, SessionUsage
from harness_cli.report import render_all, render_current


def test_render_all_zero_window_says_no_filter_not_0h():
    # --window-hours 0 means "disable the filter" (per cli.py's own help text) - the
    # text output must not misleadingly say "modified in the last 0h" for that case.
    session = SessionUsage(
        transcript_path="p", account="default", project_dir="d", session_id="s"
    )
    out = render_all([session], 0)
    assert "0h" not in out
    assert "no window filter" in out.lower()


def test_render_all_nonzero_window_states_the_hours():
    session = SessionUsage(
        transcript_path="p", account="default", project_dir="d", session_id="s"
    )
    out = render_all([session], 5)
    assert "last 5h" in out


def test_render_current_strips_terminal_escape_from_error_text():
    # A malicious/corrupted transcript's error text could smuggle an ANSI/OSC escape
    # sequence (e.g. \x1b]52;... clipboard control) - text-mode output must strip it.
    session = SessionUsage(
        transcript_path="p", account="default", project_dir="d", session_id="s"
    )
    session.error_events.append(
        ApiErrorEvent(
            timestamp="2026-09-07T10:00:00.000Z",
            category="other",
            text="hi\x1b]52;c;ZXZpbA==\x07bye",
        )
    )
    out = render_current(session, "test", 5)
    assert "\x1b" not in out
    assert "hi" in out and "bye" in out


def test_render_current_strips_terminal_escape_from_model_name():
    session = SessionUsage(
        transcript_path="p", account="default", project_dir="d", session_id="s"
    )
    session.models.add("claude\x1b]0;pwned\x07-sonnet-5")
    out = render_current(session, "test", 5)
    assert "\x1b" not in out


def test_render_current_strips_lone_surrogate_without_crashing():
    # json.loads accepts an escape like "\ud800" with no matching low surrogate; Python
    # stores it in the str but it can't be encoded to UTF-8, so printing it raw would
    # crash with UnicodeEncodeError. Must be stripped, and rendering must not crash.
    session = SessionUsage(
        transcript_path="p", account="default", project_dir="d", session_id="s"
    )
    session.error_events.append(
        ApiErrorEvent(
            timestamp="2026-09-07T10:00:00.000Z",
            category="other",
            text="hi\ud800bye",
        )
    )
    out = render_current(session, "test", 5)
    out.encode("utf-8")  # must not raise
    assert "hi" in out and "bye" in out


def test_render_current_strips_c1_escape_from_error_text():
    # \x9d is a bare C1 "OSC" introducer - some terminals honor 8-bit ECMA-48 controls
    # without needing the 7-bit ESC prefix, so C1 (\x80-\x9f) must be stripped too.
    session = SessionUsage(
        transcript_path="p", account="default", project_dir="d", session_id="s"
    )
    session.error_events.append(
        ApiErrorEvent(
            timestamp="2026-09-07T10:00:00.000Z",
            category="other",
            text="hi\x9d52;c;ZXZpbA==\x9cbye",
        )
    )
    out = render_current(session, "test", 5)
    assert "\x9d" not in out and "\x9c" not in out
    assert "hi" in out and "bye" in out


def test_render_current_strips_terminal_escape_from_account_and_transcript_path():
    bad = "\x1b]52;c;ZXZpbA==\x07"
    session = SessionUsage(
        transcript_path=f"/tmp/{bad}.jsonl", account=f"account-{bad}", project_dir="d",
        session_id="s",
    )
    out = render_current(session, "test", 5)
    assert "\x1b" not in out


def test_render_all_strips_terminal_escape_from_account_name():
    bad = "\x1b]52;c;ZXZpbA==\x07"
    session = SessionUsage(
        transcript_path="p", account=f"account-{bad}", project_dir="d", session_id="s"
    )
    out = render_all([session], 5)
    assert "\x1b" not in out


def test_render_all_error_breakdown_picks_the_true_latest_offset_timestamp():
    # Same offset-vs-Z instant trap as transcripts.py: "...+14:00" can look lexically
    # LATER than a "...Z" timestamp while being an earlier real instant.
    session = SessionUsage(
        transcript_path="p", account="default", project_dir="d", session_id="s"
    )
    session.error_events = [
        ApiErrorEvent("2026-09-07T11:57:28.000Z", "rate_limit", "newer", "new-hint"),
        ApiErrorEvent("2026-09-07T12:57:28.000+14:00", "rate_limit", "older", "old-hint"),
    ]
    out = render_all([session], 5)
    assert "last 2026-09-07T11:57:28.000Z" in out
    assert "new-hint" in out
    assert "old-hint" not in out
