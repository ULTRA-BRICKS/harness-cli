"""harness — observed usage/limit signals for local agent harnesses.

Argparse + dispatch only; behavior lives in the sibling modules (discovery, transcripts,
report). Stdlib-only at import time so `harness --help` never needs a dependency.
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from harness_cli import __version__


# Generous upper bound (~1,000 years) - comfortably below the point where
# `timedelta(hours=...)` itself overflows (`OverflowError: Python int too large to
# convert to C int`, reproduced with e.g. --window-hours 1e308, which passes a naive
# "is it finite" check but is still nonsensical as an hour count).
_MAX_WINDOW_HOURS = 24 * 365 * 1000


def _window_hours(value: str) -> float:
    """argparse type for --window-hours: finite and within a sane, non-negative range."""
    parsed = float(value)
    if math.isnan(parsed) or math.isinf(parsed) or parsed < 0 or parsed > _MAX_WINDOW_HOURS:
        raise argparse.ArgumentTypeError(
            f"--window-hours must be a finite number in [0, {_MAX_WINDOW_HOURS}] "
            f"(got {value!r})"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description=(
            "Observed token/message/api-error signals from local agent harness transcripts. "
            "Currently supports Claude Code; other harnesses (opencode/omp/codex) can add "
            "their own source module later (see harness_cli/transcripts.py)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"harness-cli {__version__}")
    sub = parser.add_subparsers(dest="command")

    limits = sub.add_parser("limits", help="Report observed usage for Claude Code sessions.")
    scope = limits.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--current", action="store_true", help="Just the current session's own transcript."
    )
    scope.add_argument(
        "--all", action="store_true", help="Every known account/session on this machine."
    )
    limits.add_argument(
        "--window-hours",
        type=_window_hours,
        default=5.0,
        help="For --all: only scan transcripts modified within this many hours (default: 5, "
        "matching Claude Code's rolling session window). 0 disables the filter.",
    )
    limits.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "limits":
        return _run_limits(args)

    parser.print_help()
    return 1


def _scan_all_sessions(discovery, parse_transcript, window_hours: float) -> list:
    """Scan every account for transcripts, filtered to the window by REAL content time.

    File mtime is used as a cheap pre-filter (skip files untouched in the window before
    paying to parse them), then each session's actual last_timestamp is re-checked after
    parsing and anything still stale is dropped - this catches the observed case of a
    fresh mtime with stale content (some external process touching the file without
    appending). It does NOT catch the inverse (stale mtime, fresh content - possible if
    something rewrites a file's bytes while preserving its old mtime, e.g. certain backup
    restores); that's a known, accepted gap of the mtime pre-filter, documented in
    README.md, not silently claimed away.
    """
    from datetime import datetime, timedelta, timezone

    from harness_cli.timeutil import parse_iso_timestamp

    sessions = []
    modified_after = None
    cutoff = None
    if window_hours > 0:
        modified_after = time.time() - window_hours * 3600
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    for config_dir in discovery.discover_account_dirs():
        account = discovery.account_label(config_dir)
        for path in discovery.find_transcripts(config_dir, modified_after=modified_after):
            try:
                session = parse_transcript(path, account)
            except OSError:
                continue
            if cutoff is not None:
                last_seen = parse_iso_timestamp(session.last_timestamp)
                if last_seen is not None and last_seen < cutoff:
                    continue
            sessions.append(session)
    return sessions


def _run_limits(args: argparse.Namespace) -> int:
    # Imported lazily: only `limits` needs the transcript-scanning modules, keeping
    # `harness --help` / `harness --version` on the stdlib-only fast path.
    from harness_cli import discovery, report
    from harness_cli.transcripts import parse_transcript

    if args.current:
        path, how = discovery.find_current_transcript()
        session = None
        if path is not None:
            config_dir = discovery.current_config_dir()
            account = discovery.account_label(config_dir)
            try:
                session = parse_transcript(path, account)
            except OSError:
                # Resolved a path, but it vanished/rotated before we could open it - same
                # race the --all path already tolerates. Report "no transcript", not crash.
                how = f"{how} (transcript vanished before it could be read)"
        if args.json:
            print(report.current_to_json(session, how))
        else:
            print(report.render_current(session, how, args.window_hours))
        return 0 if session is not None else 1

    # --all
    sessions = _scan_all_sessions(discovery, parse_transcript, args.window_hours)

    if args.json:
        print(report.sessions_to_json(sessions))
    else:
        print(report.render_all(sessions, args.window_hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
