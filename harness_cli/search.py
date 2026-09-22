"""Search parent-session transcripts for user messages matching a query.

Accessed via: `harness search QUERY [--days N]` -> cli._run_search -> this module.

Assumptions: only parent-session jsonl files are scanned (not nested subagent
transcripts); HOME is the scan root; a message whose timestamp does not parse is
skipped rather than aborting the scan.
"""

from __future__ import annotations

import json
import re
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


@dataclass(frozen=True)
class TranscriptIndex:
    """One transcript's indexed content for `paths_matching_id`/`paths_matching_text`.

    `id_tokens` is the EXACT set of `"#<digits>"` tokens found in the file's user text - a set
    membership check against this can never confuse ticket `#2` with `#21`/`#200` the way a
    plain substring test would (review finding: `"#2" in "...mentions #21..."` is `True` for a
    naive substring check, silently attributing an unrelated session's tokens to the wrong
    ticket - and unconditionally, not just across a repo boundary). `text` is the full
    lowercased concatenation, kept for a title/phrase substring check, which has no equivalent
    digit-boundary ambiguity.
    """

    id_tokens: frozenset[str]
    text: str


_ID_TOKEN_RE = re.compile(r"#\d+")


def build_transcript_corpus() -> dict[Path, TranscriptIndex]:
    """One full pass over every local CC/OMP parent-session transcript, building a
    path -> `TranscriptIndex` index.

    `search_user_messages` rescans every transcript from scratch on every call - fine for the
    interactive `search` command's single query, wasteful for a caller (`harness_cli.stats`)
    that needs to test MANY different needles (one or more per product ticket) against the SAME
    corpus in one run. Build the index once here, then test needles against it in memory via
    `paths_matching_id`/`paths_matching_text` - turns what would be one full-corpus scan per
    needle into one scan total, and (for the common `#<id>` needle) an O(1) set lookup instead
    of an O(corpus-bytes) substring scan per ticket.
    No `days`/staleness filter (unlike `search_user_messages`): a ticket's own history can be
    arbitrarily old, and this module never guesses which window matters.
    """
    corpus: dict[Path, TranscriptIndex] = {}

    def _index(paths, extract) -> None:
        for path in paths:
            texts = []
            try:
                for entry in _iter_entries(path):
                    text = extract(entry)
                    if isinstance(text, str) and text:
                        texts.append(text)
            except OSError:
                continue
            if texts:
                combined = " ".join(texts).lower()
                corpus[path] = TranscriptIndex(
                    id_tokens=frozenset(_ID_TOKEN_RE.findall(combined)), text=combined
                )

    for config_dir in discovery.discover_account_dirs():
        _index(discovery.find_parent_session_files(config_dir / "projects"), extract_cc_user_text)

    _index(
        discovery.find_parent_session_files(discovery.omp_sessions_root()), extract_omp_user_text
    )
    return corpus


def paths_matching_id(corpus: dict[Path, TranscriptIndex], ticket_id: str) -> list[Path]:
    """Every transcript path whose text mentions the EXACT token `"#<ticket_id>"` - never a
    substring false-positive (`"#2"` never matches a file that only mentions `"#21"`)."""
    token = f"#{ticket_id}"
    return [path for path, idx in corpus.items() if token in idx.id_tokens]


def paths_matching_text(corpus: dict[Path, TranscriptIndex], needle: str) -> list[Path]:
    """Every transcript path whose full indexed text contains `needle` (case-insensitive -
    `build_transcript_corpus` already lowercases its index; pass a lowercased `needle`). For a
    title/phrase needle, which has no digit-boundary ambiguity to guard against."""
    return [path for path, idx in corpus.items() if needle in idx.text]


def render_hits(hits: list[Hit]) -> str:
    if not hits:
        return "No matching user messages."
    blocks = []
    for hit in hits:
        text = _sanitize(hit.text)
        blocks.append(f"{hit.timestamp}  {hit.source}  {hit.path}\n{text}\n")
    return "\n".join(blocks)
