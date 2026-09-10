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


def _add_tasks_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    tasks = sub.add_parser(
        "tasks",
        help="Local process-ticket tracker (not GitHub issues).",
        description=("Create, list, and close local process tickets. Never files a GitHub issue."),
    )
    tasks_sub = tasks.add_subparsers(dest="tasks_command")
    created = tasks_sub.add_parser("new", help="Create a local process ticket.")
    created.add_argument("--title", required=True, help="Ticket title.")
    created.add_argument("--body", default="", help="Optional ticket body.")
    listed = tasks_sub.add_parser("list", help="List local process tickets.")
    listed.add_argument("--all", action="store_true", help="Include done tickets.")
    listed.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    done = tasks_sub.add_parser("done", help="Mark a local process ticket done.")
    done.add_argument("ticket_id", type=int, metavar="ID", help="Ticket id.")
    return tasks


def _add_agents_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    agents = sub.add_parser(
        "agents",
        help="List live/parked local agents, pending questions, and harness tasks.",
        description=(
            "Disk-only landscape of OMP and Claude Code sessions, tg-ctl questions, "
            "and local harness tasks. Live vs parked uses mtime (under 10 minutes is live)."
        ),
    )
    agents.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    agents.add_argument(
        "--all",
        action="store_true",
        help="Disable the 24-hour age window and include done tasks.",
    )
    agents.add_argument(
        "--live",
        action="store_true",
        help=(
            "Only status=='live' rows; also resolves cwd/session_id via a live file-read "
            "for kinds whose handler supports it (currently omp)."
        ),
    )
    agents_sub = agents.add_subparsers(dest="agents_command")
    web = agents_sub.add_parser(
        "web",
        help="Serve a live landscape page (default http://127.0.0.1:7888).",
    )
    web.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    web.add_argument(
        "--port",
        type=int,
        default=7888,
        help="Bind port (default 7888; 0 = ephemeral).",
    )
    web.add_argument("--no-open", action="store_true", help="Do not open a browser.")
    return agents


def _add_nudge_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    nudge = sub.add_parser(
        "nudge",
        help="Send a live nudge message to one local agent (universal per-kind adapter API).",
        description=(
            "Send MESSAGE to the live agent identified by AGENT_ID (the `id` field from a "
            "`harness agents` row: harness:kind:path). Per-kind support lives in "
            "harness_cli/nudge.py's KIND_HANDLERS registry; a kind with no handler wired "
            "returns a documented reason, never a silent no-op."
        ),
    )
    nudge.add_argument(
        "agent_id", metavar="AGENT_ID", help="Agent id from `harness agents` (harness:kind:path)."
    )
    nudge.add_argument("message", metavar="MESSAGE", help="Text to send to the agent.")
    nudge.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    nudge.add_argument(
        "--timeout-ms",
        type=int,
        default=120000,
        help="Timeout in milliseconds for the final send call (default 120000).",
    )
    return nudge


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

    parser.tasks_parser = _add_tasks_parser(sub)
    parser.agents_parser = _add_agents_parser(sub)
    parser.nudge_parser = _add_nudge_parser(sub)
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
    if args.command == "tasks":
        if getattr(args, "tasks_command", None) is None:
            parser.tasks_parser.print_help()
            return 0
        return _run_tasks(args)
    if args.command == "agents":
        return _run_agents(args)
    if args.command == "nudge":
        return _run_nudge(args)

    parser.print_help()
    return 1


def _run_search(args: argparse.Namespace) -> int:
    # Imported lazily: keep `harness --help` / `harness --version` on the stdlib-only
    # fast path, matching _run_limits.
    from harness_cli.search import render_hits, search_user_messages

    print(render_hits(search_user_messages(args.query, args.days)))
    return 0


def _run_tasks(args: argparse.Namespace) -> int:
    # Imported lazily: keep `harness --help` / `harness --version` on the stdlib-only
    # fast path, matching _run_limits / _run_search.
    from harness_cli.tasks import run

    return run(args)


def _run_agents(args: argparse.Namespace) -> int:
    # Imported lazily: keep `harness --help` / `harness --version` on the stdlib-only
    # fast path, matching _run_limits / _run_search / _run_tasks.
    if getattr(args, "agents_command", None) == "web":
        from harness_cli.agents_web import run as run_web

        return run_web(args)
    from harness_cli.agents import run

    return run(args)


def _run_nudge(args: argparse.Namespace) -> int:
    # Imported lazily: keep `harness --help` / `harness --version` on the stdlib-only
    # fast path, matching _run_limits / _run_search / _run_task / _run_agents.
    from harness_cli.nudge import run

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
