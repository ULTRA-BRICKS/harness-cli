"""Disk-only landscape of local OMP/CC agents, tg-ctl questions, and harness tasks.

Accessed via: `harness agents` -> cli._run_agents -> this module. The web UI is a
sibling (`agents_web.py`) lazy-imported from `_run_agents` when the subcommand is
`web`.

Assumptions: never walks a real developer HOME in tests (callers monkeypatch HOME
and HARNESS_* dirs). Live vs parked is an mtime heuristic (under 10 minutes is
live), never a process-liveness API. Missing or corrupt question files yield an
empty list, not an error. `req.toolInput` is never copied into the landscape.
`last_text` is redacted before assignment (Authorization headers, Bearer/OAuth,
sk-/ghp-/xox-like tokens); redact failure yields empty text, never the original.

Past bugs: `_sanitize` only stripped controls, so a pasted `Authorization: Bearer`
snippet leaked through `harness agents --json`, HTML, and SSE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from harness_cli import discovery
from harness_cli.report import _sanitize
from harness_cli.tasks import list_tickets
from harness_cli.transcripts import _open_transcript_atomically

LIVE_SECONDS = 10 * 60
DEFAULT_WINDOW_HOURS = 24
LAST_TEXT_MAX = 200
HEURISTIC = "Live vs parked: mtime under 10 minutes is live, else parked."
_QUESTION_REQ_FIELDS = (
    "question",
    "title",
    "kind",
    "agent",
    "sessionName",
    "paneId",
    "cwd",
    "subagent",
)
_SECRET_RES = (
    re.compile(r"(?i)authorization\s*:\s*\S+(?:\s+\S+)?"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+=/]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[a-zA-Z]?-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-]+=*"),
)
_last_text_cache: dict[tuple[str, float], str] = {}


def questions_dir() -> Path:
    explicit = os.environ.get("HARNESS_QUESTIONS_DIR")
    if explicit:
        return Path(explicit)
    return Path.home() / ".config" / "tg-cli"


def _iso_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(mtime: float, now_ts: float) -> str:
    if now_ts - mtime < LIVE_SECONDS:
        return "live"
    return "parked"


def _entry_text(entry: dict) -> str | None:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else entry.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "".join(texts) or None


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_RES:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _read_last_snippet(path: Path) -> str:
    snippet = ""
    try:
        with _open_transcript_atomically(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError, RecursionError):
                    continue
                if not isinstance(entry, dict):
                    continue
                text = _entry_text(entry)
                if text:
                    snippet = text
    except OSError:
        return ""
    return snippet


def _finish_last_text(snippet: str) -> str:
    cleaned = " ".join(_sanitize(snippet).split())
    cleaned = _redact_secrets(cleaned)
    if len(cleaned) > LAST_TEXT_MAX:
        return cleaned[: LAST_TEXT_MAX - 1] + "…"
    return cleaned


def _last_text(path: Path) -> str:
    mtime = discovery._safe_mtime(path)
    if mtime is None:
        return ""
    key = (str(path), mtime)
    cached = _last_text_cache.get(key)
    if cached is not None:
        return cached
    try:
        cleaned = _finish_last_text(_read_last_snippet(path))
    except (TypeError, ValueError, re.error):
        cleaned = ""
    stale = [old for old in _last_text_cache if old[0] == key[0] and old != key]
    for old in stale:
        del _last_text_cache[old]
    _last_text_cache[key] = cleaned
    return cleaned


def _agent_row(
    path: Path, harness: str, kind: str, now_ts: float, modified_after: float | None
) -> dict | None:
    mtime = discovery._safe_mtime(path)
    if mtime is None:
        return None
    if modified_after is not None and mtime < modified_after:
        return None
    return {
        "id": f"{harness}:{kind}:{path}",
        "name": _sanitize(path.stem),
        "harness": harness,
        "kind": kind,
        "path": str(path),
        "mtime": _iso_mtime(mtime),
        "status": _status(mtime, now_ts),
        "last_text": _last_text(path),
    }


def collect_omp_agents(now_ts: float, modified_after: float | None) -> list[dict]:
    root = discovery.omp_sessions_root()
    if not discovery._safe_is_real_dir(root):
        return []
    rows: list[dict] = []
    for path in discovery._iter_jsonl_files(root):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) == 2:
            kind = "parent"
        elif len(rel.parts) == 3:
            kind = "subagent"
        else:
            continue
        row = _agent_row(path, "omp", kind, now_ts, modified_after)
        if row is not None:
            rows.append(row)
    return rows


def collect_cc_agents(now_ts: float, modified_after: float | None) -> list[dict]:
    rows: list[dict] = []
    for config_dir in discovery.discover_account_dirs():
        for path in discovery.find_transcripts(config_dir, modified_after=modified_after):
            kind = "subagent" if "subagents" in path.parts else "parent"
            row = _agent_row(path, "claude", kind, now_ts, modified_after)
            if row is not None:
                rows.append(row)
    return rows


def _question_row(item: dict) -> dict | None:
    req = item.get("req")
    if not isinstance(req, dict):
        return None
    row = {field: _sanitize(str(req[field])) if field in req else "" for field in _QUESTION_REQ_FIELDS}
    at = item.get("at")
    row["at"] = _sanitize(str(at)) if at is not None else ""
    message_id = item.get("messageId")
    row["messageId"] = message_id if isinstance(message_id, (int, str)) else ""
    return row


def collect_questions() -> list[dict]:
    root = questions_dir()
    if not root.is_dir():
        return []
    rows: list[dict] = []
    try:
        paths = list(root.glob("tg-ctl.*.questions.json"))
    except OSError:
        return []
    for path in sorted(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(payload, dict):
            continue
        questions = payload.get("questions")
        if not isinstance(questions, list):
            continue
        for item in questions:
            if not isinstance(item, dict):
                continue
            row = _question_row(item)
            if row is not None:
                rows.append(row)
    return rows


def build_landscape(*, include_all: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    modified_after = None
    window = "all"
    if not include_all:
        modified_after = now_ts - DEFAULT_WINDOW_HOURS * 3600
        window = "24h"
    agents = collect_omp_agents(now_ts, modified_after) + collect_cc_agents(now_ts, modified_after)
    agents.sort(key=lambda row: row.get("mtime") or "", reverse=True)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": window,
        "agents": agents,
        "questions": collect_questions(),
        "tasks": list_tickets(include_done=include_all),
    }


def render_landscape(data: dict) -> str:
    window = data.get("window")
    if window == "all":
        header = "Landscape window: all (age window disabled)."
    else:
        header = "Landscape window: last 24 hours (use --all to disable)."
    lines = [header, HEURISTIC, "", "Agents"]
    agents = data.get("agents") or []
    if not agents:
        lines.append("  (none)")
    for row in agents:
        lines.append(
            f"  {row.get('status', ''):6} {row.get('harness', ''):6} "
            f"{row.get('kind', ''):8} {_sanitize(str(row.get('name', '')))}  "
            f"{_sanitize(str(row.get('last_text') or ''))}"
        )
    lines.extend(["", "Questions"])
    questions = data.get("questions") or []
    if not questions:
        lines.append("  (none)")
    for row in questions:
        lines.append(
            f"  {_sanitize(str(row.get('agent') or ''))}  "
            f"{_sanitize(str(row.get('question') or row.get('title') or ''))}"
        )
    lines.extend(["", "Tasks"])
    tasks = data.get("tasks") or []
    if not tasks:
        lines.append("  (none)")
    for ticket in tasks:
        lines.append(
            f"  {_sanitize(str(ticket.get('id', '')))} "
            f"{_sanitize(str(ticket.get('state', '')))} "
            f"{_sanitize(str(ticket.get('title', '')))}"
        )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    include_all = bool(getattr(args, "all", False))
    data = build_landscape(include_all=include_all)
    if getattr(args, "json", False):
        print(json.dumps(data))
        return 0
    print(render_landscape(data))
    return 0
