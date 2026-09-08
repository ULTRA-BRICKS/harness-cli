"""Search parent-session transcripts for user messages matching a query.

Accessed via: `harness search QUERY [--days N]` -> cli._run_search -> this module.

Assumptions: only parent-session jsonl files are scanned (not nested subagent
transcripts); HOME is the scan root; a message whose timestamp does not parse is
skipped rather than aborting the scan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness_cli import discovery
from harness_cli.report import _sanitize
from harness_cli.timeutil import parse_iso_timestamp
from harness_cli.transcripts import _open_transcript_atomically

_SKIP_CC_FLAGS = ("isMeta", "isSidechain", "isCompactSummary")


@dataclass(frozen=True)
class Hit:
    timestamp: str
    when: datetime
    source: str
    path: Path
    text: str


def _content_text(content) -> str | None:
    """Return joined user text, or None if this turn is not printable user content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # Tool-result "user" turns are Claude Code plumbing, not a human prompt.
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "".join(texts)


def extract_cc_user_text(entry: dict) -> str | None:
    if entry.get("type") != "user":
        return None
    if any(entry.get(flag) is True for flag in _SKIP_CC_FLAGS):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    return _content_text(message.get("content"))


def extract_omp_user_text(entry: dict) -> str | None:
    if entry.get("type") != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    if message.get("role") != "user":
        return None
    # Subagent briefs are typed as role=user with attribution=agent.
    if message.get("attribution") == "agent":
        return None
    return _content_text(message.get("content"))


def _iter_entries(path: Path):
    with _open_transcript_atomically(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError, RecursionError):
                continue
            if isinstance(entry, dict):
                yield entry


def _hits_from_file(
    path: Path,
    source: str,
    extract,
    needle: str,
    cutoff: datetime | None,
) -> list[Hit]:
    hits: list[Hit] = []
    try:
        entries = _iter_entries(path)
        for entry in entries:
            text = extract(entry)
            if not isinstance(text, str) or needle not in text.lower():
                continue
            timestamp = entry.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            when = parse_iso_timestamp(timestamp)
            if when is None:
                continue
            if cutoff is not None and when < cutoff:
                continue
            hits.append(Hit(timestamp=timestamp, when=when, source=source, path=path, text=text))
    except OSError:
        return []
    return hits


def search_user_messages(query: str, days: float) -> list[Hit]:
    now = datetime.now(timezone.utc)
    cutoff = None
    modified_after = None
    if days > 0:
        cutoff = now - timedelta(days=days)
        modified_after = cutoff.timestamp()

    needle = query.lower()
    hits: list[Hit] = []

    for config_dir in discovery.discover_account_dirs():
        root = config_dir / "projects"
        for path in discovery.find_parent_session_files(root, modified_after=modified_after):
            hits.extend(_hits_from_file(path, "cc", extract_cc_user_text, needle, cutoff))

    omp_root = discovery.omp_sessions_root()
    for path in discovery.find_parent_session_files(omp_root, modified_after=modified_after):
        hits.extend(_hits_from_file(path, "omp", extract_omp_user_text, needle, cutoff))

    hits.sort(key=lambda hit: hit.when, reverse=True)
    return hits


def render_hits(hits: list[Hit]) -> str:
    if not hits:
        return "No matching user messages."
    blocks = []
    for hit in hits:
        text = _sanitize(hit.text)
        blocks.append(f"{hit.timestamp}  {hit.source}  {hit.path}\n{text}\n")
    return "\n".join(blocks)
