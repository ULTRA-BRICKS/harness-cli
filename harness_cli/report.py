"""Format SessionUsage data as text or JSON. Stdlib-only."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from harness_cli.model import SessionUsage
from harness_cli.timeutil import parse_iso_timestamp

DEFAULT_WINDOW_HOURS = 5  # mirrors Claude Code's rolling 5-hour session window
_UNPARSEABLE_TIMESTAMP_FLOOR = datetime.min.replace(tzinfo=timezone.utc)


def _timestamp_sort_key(timestamp: str) -> datetime:
    """Sort key for chronological ordering by real INSTANT, not raw-string lexical order.

    Mirrors transcripts.py's _is_later/_is_earlier: two valid ISO-8601 timestamps in
    different UTC offsets don't sort correctly as text. An unparseable timestamp sorts as
    the oldest possible value (never treated as "most recent") - deterministic, not a
    crash, not fabricated precision.
    """
    return parse_iso_timestamp(timestamp) or _UNPARSEABLE_TIMESTAMP_FLOOR

# C0 controls (incl. ESC 0x1B, the start of every 7-bit ANSI/OSC escape sequence), DEL,
# C1 controls (\x80-\x9f - terminals that honor 8-bit ECMA-48 controls treat e.g. \x9d as
# an OSC introducer on its own, no ESC needed), and lone UTF-16 surrogates (\ud800-\udfff
# - `json.loads` accepts an escape like "\ud800" with no matching low surrogate, which
# Python happily stores in a str but then can't encode to UTF-8 for stdout, crashing
# `print()` with UnicodeEncodeError - not a display exploit, but still a real crash from
# untrusted transcript content, so stripped here too).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def _sanitize(text: str) -> str:
    """Strip terminal control characters and lone surrogates before printing text.

    JSON output carries the raw value untouched - this is ONLY for the text-mode
    renderers below, since a malicious/corrupted transcript line (model name, error
    message, timestamp) could otherwise smuggle an ANSI/OSC escape sequence (e.g. OSC 52
    clipboard control) into a terminal that then executes it just by being displayed, or
    an unencodable lone surrogate that would crash `print()` outright.
    """
    return _CONTROL_CHARS_RE.sub("", text)


def sessions_to_json(sessions: list[SessionUsage]) -> str:
    return json.dumps([s.to_dict() for s in sessions], indent=2)


def current_to_json(session: SessionUsage | None, how: str) -> str:
    """JSON for `limits --current`: always carries the resolution `how`, matching text
    output's own contract (see render_current) - a caller must be able to tell whether
    this was a direct session-id match or the "most recently modified" fallback from
    JSON too, not just from text.
    """
    return json.dumps(
        {"resolution": how, "session": session.to_dict() if session else None}, indent=2
    )


def render_current(session: SessionUsage | None, how: str, window_hours: float) -> str:
    if session is None:
        return (
            "No transcript found for the current session.\n"
            f"(resolution: {how})\n"
            "This can happen outside an active Claude Code session, or on a machine with "
            "no local transcript history yet."
        )
    first_activity = _sanitize(session.first_timestamp) if session.first_timestamp else "unknown"
    last_activity = _sanitize(session.last_timestamp) if session.last_timestamp else "unknown"
    models_seen = _sanitize(", ".join(sorted(session.models))) or "none recorded"
    lines = [
        f"Current session ({how}):",
        f"  account:        {_sanitize(session.account)}",
        f"  transcript:     {_sanitize(session.transcript_path)}",
        f"  session id:     {_sanitize(session.session_id)}",
        f"  window:         observed usage is NOT windowed to {window_hours}h "
        f"(single-file scan; see 'harness limits --all' for cross-file rollups)",
        f"  first activity: {first_activity}",
        f"  last activity:  {last_activity}",
        f"  messages:       {session.message_count}",
        f"  models seen:    {models_seen}",
        f"  assistant turns with usage data: {session.totals.assistant_turns}",
        f"  input tokens:              {session.totals.input_tokens:,}",
        f"  output tokens:             {session.totals.output_tokens:,}",
        f"  cache-creation tokens:     {session.totals.cache_creation_input_tokens:,}",
        f"  cache-read tokens:         {session.totals.cache_read_input_tokens:,}",
        f"  total tokens (all above):  {session.totals.total_tokens:,}",
    ]
    lines.append(_render_error_events(session))
    return "\n".join(lines)


def render_all(sessions: list[SessionUsage], window_hours: float) -> str:
    if not sessions:
        return "No transcripts found within the scan window."

    by_account: dict[str, list[SessionUsage]] = {}
    for s in sessions:
        by_account.setdefault(s.account, []).append(s)

    if window_hours == 0:
        scope = "with no window filter (all history)"
    else:
        scope = f"last active in the last {window_hours:g}h"
    lines = [
        f"Observed usage across {len(sessions)} transcript(s) {scope}, "
        f"{len(by_account)} account(s):",
        # Token/message totals below are EACH SESSION'S FULL lifetime total, not just
        # what happened inside the window - a session touched once 10 minutes ago still
        # reports everything it has ever done. This is a session-level filter (include
        # this transcript or not), not a per-message one.
        "(totals are each included session's FULL lifetime usage, not just the window)",
        "",
    ]
    grand_total_tokens = 0
    grand_errors = 0
    for account in sorted(by_account):
        account_sessions = by_account[account]
        total_tokens = sum(s.totals.total_tokens for s in account_sessions)
        errors = sum(len(s.error_events) for s in account_sessions)
        messages = sum(s.message_count for s in account_sessions)
        grand_total_tokens += total_tokens
        grand_errors += errors
        lines.append(f"[{_sanitize(account)}] {len(account_sessions)} transcript(s)")
        lines.append(f"  messages:      {messages}")
        lines.append(f"  total tokens:  {total_tokens:,}")
        lines.append(f"  api errors:    {errors}")
        for line in _render_error_breakdown(account_sessions):
            lines.append(f"    {line}")
        most_recent = sorted(
            account_sessions, key=lambda s: _timestamp_sort_key(s.last_timestamp or ""), reverse=True
        )[:5]
        for s in most_recent:
            last = _sanitize(s.last_timestamp) if s.last_timestamp else "unknown"
            lines.append(
                f"    - {_sanitize(s.session_id[:12])}  msgs={s.message_count:<4} "
                f"tokens={s.totals.total_tokens:<9,} last={last}"
            )
        lines.append("")

    lines.append(f"TOTAL tokens observed: {grand_total_tokens:,}")
    lines.append(f"TOTAL api-error events observed: {grand_errors}")
    return "\n".join(lines)


def _render_error_breakdown(sessions: list[SessionUsage]) -> list[str]:
    """Per-category count + latest occurrence (+ its raw reset hint, if any) for an account."""
    by_category: dict[str, list] = {}
    for s in sessions:
        for event in s.error_events:
            by_category.setdefault(event.category, []).append(event)
    out = []
    for category in sorted(by_category):
        events = sorted(by_category[category], key=lambda e: _timestamp_sort_key(e.timestamp))
        latest = events[-1]
        hint = f" (hint: resets {_sanitize(latest.reset_hint)})" if latest.reset_hint else ""
        out.append(f"{category}: {len(events)}, last {_sanitize(latest.timestamp)}{hint}")
    return out


def _render_error_events(session: SessionUsage) -> str:
    if not session.error_events:
        return "  api errors:     none observed in this transcript"
    out = [f"  api errors:     {len(session.error_events)} observed"]
    for event in session.error_events[-10:]:
        hint = f" (resets {_sanitize(event.reset_hint)})" if event.reset_hint else ""
        text = _sanitize(event.text[:120])
        out.append(f"    - [{event.category}] {_sanitize(event.timestamp)}: {text}{hint}")
    return "\n".join(out)
