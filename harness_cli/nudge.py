"""Per-kind nudge dispatch — the one place that knows each harness's own quirk.

Accessed via: `harness nudge AGENT_ID MESSAGE` -> cli._run_nudge -> this module.
`harness_cli/agents.py --live` also reads this registry (`nudge_supported`,
`resolve_target_for`) so there is a single source of truth for both "can I
nudge this kind" and "how do I find its live cwd/session id" - no duplicate
session-file parsing logic.

Mirrors rig-cli's riglib/harness_mode.py `HARNESS_MODES` + `HARNESS_MODE_NA`
pattern: one frozen dataclass per kind, unsupported kinds carry a documented
reason, never a silent no-op.

Assumptions: AGENT_ID is exactly the `id` field a `harness agents` row
produced (`f"{harness}:{kind}:{path}"`, split with maxsplit=2 - POSIX paths
never contain `:`). A `harness` value absent from KIND_HANDLERS is
"not-found", not "unsupported": `harness_cli/agents.py` doesn't collect rows
for it yet, so there is no live row to nudge in the first place.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness_cli import discovery, omp_acp

# Mirrors agents.LIVE_SECONDS. Duplicated (not imported) to avoid a
# harness_cli.agents <-> harness_cli.nudge import cycle: agents.py already
# imports this module for the per-kind registry, so this module must not
# import agents.py back.
_LIVE_SECONDS = 10 * 60

_EXIT_CODES = {"failed": 1, "unsupported": 2, "not-found": 3}


@dataclass(frozen=True)
class KindHandler:
    """One harness's nudge support: how to resolve a live target, and how to send."""

    resolve_target: Callable[[Path], dict | None]
    nudge: Callable[[dict, str, float], dict] | None
    unsupported_reason: str | None = None


KIND_HANDLERS: dict[str, KindHandler] = {
    "omp": KindHandler(
        resolve_target=omp_acp.resolve_target,
        nudge=omp_acp.nudge,
    ),
    "claude": KindHandler(
        resolve_target=lambda _path: None,
        nudge=None,
        unsupported_reason=(
            "Claude Code has no local push/RPC channel this process can reach - "
            "only tg-ctl's own passive skill-discovery path exists today."
        ),
    ),
}


@dataclass(frozen=True)
class NudgeResult:
    ok: bool
    error: str | None = None
    reason: str | None = None  # "unsupported" | "failed" | "not-found" | None (ok)

    def to_json(self) -> dict:
        if self.ok:
            return {"ok": True}
        return {"ok": False, "error": self.error, "reason": self.reason}

    def exit_code(self) -> int:
        return 0 if self.ok else _EXIT_CODES[self.reason]


def nudge_supported(harness: str) -> bool:
    """Cheap, O(1), no file I/O - safe to compute for every `agents` row."""
    handler = KIND_HANDLERS.get(harness)
    return handler is not None and handler.nudge is not None


def resolve_target_for(harness: str, path: Path) -> dict | None:
    handler = KIND_HANDLERS.get(harness)
    if handler is None:
        return None
    return handler.resolve_target(path)


def parse_agent_id(agent_id: str) -> tuple[str, str, str] | None:
    parts = agent_id.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def send_nudge(agent_id: str, message: str, timeout_seconds: float) -> NudgeResult:
    parsed = parse_agent_id(agent_id)
    if parsed is None:
        return NudgeResult(ok=False, error=f"malformed agent id: {agent_id!r}", reason="not-found")
    harness, _kind, path_str = parsed
    handler = KIND_HANDLERS.get(harness)
    if handler is None:
        return NudgeResult(
            ok=False, error=f"unknown agent harness: {harness!r}", reason="not-found"
        )

    path = Path(path_str)
    mtime = discovery._safe_mtime(path)
    if mtime is None:
        return NudgeResult(
            ok=False, error=f"agent session file not found: {path}", reason="not-found"
        )
    if time.time() - mtime >= _LIVE_SECONDS:
        return NudgeResult(
            ok=False,
            error=(
                f"agent is not currently live (no activity in the last "
                f"{_LIVE_SECONDS // 60} minutes)"
            ),
            reason="not-found",
        )

    if handler.nudge is None:
        return NudgeResult(
            ok=False,
            error=handler.unsupported_reason or f"nudge not supported for harness {harness!r}",
            reason="unsupported",
        )

    target = handler.resolve_target(path)
    if target is None:
        return NudgeResult(
            ok=False,
            error=f"could not resolve a live session target for {path}",
            reason="not-found",
        )

    result = handler.nudge(target, message, timeout_seconds)
    if result.get("ok"):
        return NudgeResult(ok=True)
    return NudgeResult(ok=False, error=result.get("error") or "nudge failed", reason="failed")


def run(args: argparse.Namespace) -> int:
    timeout_seconds = max(getattr(args, "timeout_ms", 120000), 0) / 1000.0
    result = send_nudge(args.agent_id, args.message, timeout_seconds)
    if getattr(args, "json", False):
        print(json.dumps(result.to_json()))
    elif result.ok:
        print("sent")
    else:
        print(result.error or "nudge failed", file=sys.stderr)
    return result.exit_code()
