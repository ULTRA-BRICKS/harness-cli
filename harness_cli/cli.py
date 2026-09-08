"""harness — rate-limit proximity for local agent harness accounts.

Argparse + dispatch only; behavior lives in sibling modules (limits, search, tasks).
Stdlib-only at import time so `harness --help` never needs a dependency. Network
collectors are lazy-imported inside `_run_limits`.
"""

from __future__ import annotations

import argparse
import math
import sys

from harness_cli import __version__


# Generous upper bound (~1,000 years) - comfortably below the point where
# `timedelta(hours=...)` itself overflows (`OverflowError: Python int too large to
# convert to C int`, reproduced with e.g. --window-hours 1e308, which passes a naive
# "is it finite" check but is still nonsensical as an hour count).
_MAX_WINDOW_HOURS = 24 * 365 * 1000


_MAX_DAYS = 365 * 1000


def _finite_non_negative(value: str, flag: str, maximum: float) -> float:
    """argparse type: finite and within a sane, non-negative range."""
    parsed = float(value)
    if math.isnan(parsed) or math.isinf(parsed) or parsed < 0 or parsed > maximum:
        raise argparse.ArgumentTypeError(
            f"{flag} must be a finite number in [0, {maximum}] (got {value!r})"
        )
    return parsed


def _window_hours(value: str) -> float:
    """argparse type for --window-hours: finite and within a sane, non-negative range."""
    return _finite_non_negative(value, "--window-hours", _MAX_WINDOW_HOURS)


def _days(value: str) -> float:
    """argparse type for --days: same finite non-negative idea as --window-hours."""
    return _finite_non_negative(value, "--days", _MAX_DAYS)


def _add_task_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    task = sub.add_parser(
        "task",
        help="Local process-ticket tracker (not GitHub issues).",
        description=(
            "Create, list, and close local process tickets. Never files a GitHub issue."
        ),
    )
    task_sub = task.add_subparsers(dest="task_command")
    created = task_sub.add_parser("new", help="Create a local process ticket.")
    created.add_argument("--title", required=True, help="Ticket title.")
    created.add_argument("--body", default="", help="Optional ticket body.")
    listed = task_sub.add_parser("list", help="List local process tickets.")
    listed.add_argument("--all", action="store_true", help="Include done tickets.")
    listed.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    done = task_sub.add_parser("done", help="Mark a local process ticket done.")
    done.add_argument("ticket_id", type=int, metavar="ID", help="Ticket id.")
    return task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description=(
            "Show real rate-limit proximity (percent used, reset countdown, pace) "
            "for Claude Code, Codex, z.ai, x.ai, and kimi accounts on this machine. "
            "Percents come from each provider's usage API — never from local "
            "transcript token counts."
        ),
    )
    parser.add_argument("--version", action="version", version=f"harness-cli {__version__}")
    sub = parser.add_subparsers(dest="command")

    limits = sub.add_parser(
        "limits",
        help="Show real rate-limit proximity per account (not transcript token tables).",
    )
    scope = limits.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--current",
        action="store_true",
        help="Proximity for the current Claude Code account (same renderer as --all).",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="Every known account/provider on this machine.",
    )
    limits.add_argument(
        "--window-hours",
        type=_window_hours,
        default=5.0,
        help="Accepted for compatibility; does not compute or fake a percent for --all. "
        "Must be a finite number in [0, 8760000].",
    )
    limits.add_argument(
        "--json",
        action="store_true",
        help="Emit the proximity model (cards + windows) as JSON.",
    )

    search = sub.add_parser(
        "search",
        help="Find user messages in Claude Code and Oh My Pi parent transcripts.",
    )
    search.add_argument(
        "query",
        help="Case-insensitive substring matched against extracted user text.",
    )
    search.add_argument(
        "--days",
        type=_days,
        default=3.0,
        help="Only messages whose timestamp is within this many days (default: 3). "
        "0 disables the filter.",
    )

    parser.task_parser = _add_task_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "limits":
        return _run_limits(args)
    if args.command == "search":
        return _run_search(args)
    if args.command == "task":
        if getattr(args, "task_command", None) is None:
            parser.task_parser.print_help()
            return 0
        return _run_task(args)

    parser.print_help()
    return 1


def _run_search(args: argparse.Namespace) -> int:
    # Imported lazily: keep `harness --help` / `harness --version` on the stdlib-only
    # fast path, matching _run_limits.
    from harness_cli.search import render_hits, search_user_messages

    print(render_hits(search_user_messages(args.query, args.days)))
    return 0


def _run_task(args: argparse.Namespace) -> int:
    # Imported lazily: keep `harness --help` / `harness --version` on the stdlib-only
    # fast path, matching _run_limits / _run_search.
    from harness_cli.tasks import run

    return run(args)


def _run_limits(args: argparse.Namespace) -> int:
    # Imported lazily: network collectors stay off the `harness --help` /
    # `harness --version` stdlib-only fast path.
    from harness_cli import limits_collect, limits_render

    if args.current:
        cards = limits_collect.collect_current()
    else:
        cards = limits_collect.collect_all()

    if args.json:
        print(limits_render.cards_to_json(cards))
    else:
        color = limits_render.color_enabled(sys.stdout)
        print(limits_render.render_cards(cards, color=color))
    return 0


if __name__ == "__main__":
    sys.exit(main())
