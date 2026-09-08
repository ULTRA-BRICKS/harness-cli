"""Parse a Claude Code transcript (.jsonl) file into a SessionUsage summary.

Stdlib-only (json). Reads exactly the fields Claude Code itself writes per line - no
estimation, no fabricated numbers. A line this parser doesn't understand is skipped, not
guessed at.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

from harness_cli.model import ApiErrorEvent, SessionUsage, classify_error_text, extract_reset_hint
from harness_cli.timeutil import parse_iso_timestamp

# O_NOFOLLOW: refuse to open through a symlink at all (a "*.jsonl" symlink into another
# account's transcripts, or outside the projects tree, must not be scanned as if it were
# a real transcript here). O_NONBLOCK: opening a FIFO for read normally blocks until a
# writer appears - with this flag it returns immediately instead, so even a FIFO swapped
# in between a check and this open() can't hang the CLI. Both are POSIX-only; Windows
# lacks named FIFOs in this sense and falls back to a plain open.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_transcript_atomically(path: Path) -> io.TextIOWrapper:
    """Open `path` for reading, refusing anything that isn't a real regular file.

    Unlike a separate is_file()/is_symlink() check followed by a plain open() (which
    leaves a race window - path could be replaced by a symlink or FIFO in between), this
    validates the ACTUAL opened file descriptor via fstat, closing the entire race: what
    gets read is provably the same regular file that was just opened, never whatever a
    concurrent process swapped the name to point at afterward.
    """
    # vanished, permission denied, or (via O_NOFOLLOW) actually a symlink
    fd = os.open(path, _OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "r", encoding="utf-8", errors="replace")


def parse_transcript(path: Path, account: str) -> SessionUsage:
    """Read one transcript file front to back and summarize what's actually in it.

    Raises OSError if `path` isn't safely readable as a real regular file - vanished,
    a symlink, or a FIFO/named pipe with a spoofed ".jsonl" name (which a naive open()
    could otherwise block on forever waiting for a writer). Callers already handle
    OSError (see cli.py) as "this transcript couldn't be read", which covers this too.
    """
    session = SessionUsage(
        transcript_path=str(path),
        account=account,
        project_dir=str(path.parent),
        session_id=path.stem,
    )
    with _open_transcript_atomically(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError, RecursionError):
                # ValueError also covers json's own int-string-digit-limit guard (an
                # absurdly long numeric literal); RecursionError covers pathologically
                # deep nesting (thousands of nested arrays/objects) exceeding Python's
                # call-stack limit inside the decoder. Both are still just a malformed
                # line to skip, not a reason to abort the whole scan.
                continue
            if not isinstance(entry, dict):
                # A syntactically valid but non-object JSON line (e.g. `[]`, `"text"`,
                # `null`) - not a shape Claude Code itself writes; skip, don't crash.
                continue
            _apply_entry(session, entry)
    return session


def _update_first_last(session: SessionUsage, timestamp: str) -> None:
    """Update first/last activity by real INSTANT - only for a timestamp that PARSES.

    A candidate that fails to parse (e.g. a corrupted "zzzz" string) contributes no
    signal at all here - it must never become first/last_timestamp itself (that would
    make --window-hours's downstream filter unable to parse it either and silently keep
    a stale session, defeating the whole staleness check) and it must never be compared
    lexically against a real timestamp either. Once this guard holds, every stored
    first/last_timestamp is guaranteed parseable, so the comparisons below never need a
    string-lexical fallback.
    """
    candidate_dt = parse_iso_timestamp(timestamp)
    if candidate_dt is None:
        return

    first_dt = parse_iso_timestamp(session.first_timestamp)
    if first_dt is None or candidate_dt < first_dt:
        session.first_timestamp = timestamp

    last_dt = parse_iso_timestamp(session.last_timestamp)
    if last_dt is None or candidate_dt > last_dt:
        session.last_timestamp = timestamp


def _apply_entry(session: SessionUsage, entry: dict) -> None:
    entry_type = entry.get("type")
    is_message_entry = entry_type in ("user", "assistant")

    # Only a real user/assistant record counts as "activity" for first/last timestamp -
    # a same-turn "attachment" or other metadata record with its own fresh timestamp must
    # not be able to make stale message content look recent (this backs the --window-hours
    # staleness filter in cli.py, which trusts last_timestamp as the real content time).
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, str):
        timestamp = None  # a malformed transcript line (e.g. a numeric epoch) isn't real
    if timestamp and is_message_entry:
        # min/max by VALUE, not "first/last line seen" - a transcript isn't guaranteed to
        # be in strict chronological order (merge/restore/retry can append an older
        # record after a newer one), and --window-hours trusts last_timestamp as the true
        # most-recent activity. See _update_first_last for why this only ever stores a
        # timestamp that actually parses as a real instant.
        _update_first_last(session, timestamp)

    if is_message_entry:
        session.message_count += 1

    if entry.get("isApiErrorMessage") is True:
        session.error_events.append(_error_event(entry, timestamp))
        return

    message = entry.get("message")
    if not isinstance(message, dict):
        return

    model = message.get("model")
    if isinstance(model, str) and model and model != "<synthetic>":
        session.models.add(model)

    # "assistant turns with usage data" is the documented meaning of totals.assistant_turns
    # (see report.py) - a "user" record's own `message.usage` (if any) must not inflate it.
    usage = message.get("usage")
    if entry_type == "assistant" and isinstance(usage, dict):
        session.totals.add(usage)


def _error_event(entry: dict, timestamp: str | None) -> ApiErrorEvent:
    text = _extract_error_text(entry)
    return ApiErrorEvent(
        timestamp=timestamp or "",
        category=classify_error_text(text),
        text=text,
        reset_hint=extract_reset_hint(text),
    )


def _extract_error_text(entry: dict) -> str:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(part for part in parts if isinstance(part, str) and part)
    return ""
