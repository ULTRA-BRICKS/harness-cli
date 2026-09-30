"""Locate Claude Code account config dirs and transcript files on disk.

Stdlib-only. Two account layouts coexist on this machine (and are handled generically,
not hard-coded to one machine): a stock single-account `~/.claude`, and a multi-account
`~/.claude-accounts/account-*` layout (each with its own `projects/`). `$CLAUDE_CONFIG_DIR`
(set for the running Claude Code session) always takes priority for "current session"
resolution.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Real Claude Code session ids are UUIDs (e.g. "7bda0248-d8a4-4374-a227-dfe5945e31d0").
# Restricting to this shape closes off a path-traversal surface: an unvalidated
# $CLAUDE_CODE_SESSION_ID containing "../" could otherwise walk `project_dir / f"{id}.jsonl"`
# outside the current project and report an unrelated file as "the current session".
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def default_home() -> Path:
    return Path.home()


def _safe_mtime(path: Path) -> float | None:
    """`path.stat().st_mtime`, or None if the file vanished/became unreadable mid-scan.

    A transcript can be rotated or deleted between `rglob()` yielding it and this stat
    call; that's a race to tolerate (skip the entry), not a reason to crash the scan.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _safe_is_regular_file(path: Path) -> bool:
    """A real (non-symlink) regular file, or False - never raises.

    Checked before `is_symlink()`'s cousin `is_file()` would follow a symlink: a
    "*.jsonl" symlink pointing at another account's transcript (or outside the projects
    tree entirely) must not be scanned as if it belonged here.
    """
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _safe_is_real_dir(path: Path) -> bool:
    """A real (non-symlink) directory, or False - never raises.

    Applied to every directory this module is about to WALK (a config dir, its
    `projects/`, or a per-project dir under it) - not just the individual files inside
    it. `_safe_is_regular_file` alone only stops a symlinked *file*; a symlinked
    *directory* (e.g. `~/.claude/projects` itself pointed somewhere else) would still
    pass a plain `is_dir()` check and get walked as if it belonged here.
    """
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError:
        return False


def _iter_jsonl_files(root: Path):
    """Yield every `*.jsonl` under `root`, tolerating unreadable subdirectories.

    `Path.rglob` raises and aborts the ENTIRE walk on the first `PermissionError` it hits
    (e.g. a stale account dir with restrictive ACLs); `os.walk(..., onerror=...)` instead
    skips just that one subtree and keeps walking its siblings, which is what "scan every
    account, best-effort" should mean.
    """
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _err: None):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            candidate = Path(dirpath) / name
            # is_file() is False for a FIFO/socket/device node - a corrupted or
            # adversarial tree containing e.g. a named pipe named "x.jsonl" must not be
            # handed to open() later, which would block forever waiting for a writer.
            # (parse_transcript() re-checks this itself too, closer to the actual open.)
            if _safe_is_regular_file(candidate):
                yield candidate


def _most_recent_transcript(root: Path) -> Path | None:
    """The most recently modified `*.jsonl` under `root`, tolerating vanished files."""
    best: tuple[Path, float] | None = None
    for path in _iter_jsonl_files(root):
        mtime = _safe_mtime(path)
        if mtime is None:
            continue
        if best is None or mtime > best[1]:
            best = (path, mtime)
    return best[0] if best else None


def slugify_cwd(cwd: str) -> str:
    """Mirror Claude Code's own project-dir slug: '/' -> '-'.

    Matches the on-disk convention observed directly (e.g. `/Users/ultra/xp/rig-cli` ->
    `-Users-ultra-xp-rig-cli`), not a guess.
    """
    return cwd.replace("/", "-")


def discover_account_dirs(home: Path | None = None) -> list[Path]:
    """Return every Claude Code config dir on this machine that has a `projects/` subdir.

    Includes `~/.claude` (stock layout) and every `~/.claude-accounts/account-*` (multi-account
    layout), deduplicated by resolved path. Order: `~/.claude` first, then accounts sorted
    by name.
    """
    home = home or default_home()
    found: list[Path] = []
    seen: set[Path] = set()

    stock = home / ".claude"
    if _safe_is_real_dir(stock) and _safe_is_real_dir(stock / "projects"):
        resolved = stock.resolve()
        if resolved not in seen:
            found.append(stock)
            seen.add(resolved)

    accounts_root = home / ".claude-accounts"
    if _safe_is_real_dir(accounts_root):
        try:
            entries = sorted(accounts_root.iterdir())
        except OSError:
            # Unreadable ~/.claude-accounts itself: report what was found above, not a
            # crash - best-effort discovery, same principle as _iter_jsonl_files.
            entries = []
        for entry in entries:
            if not entry.name.startswith("account-") or not _safe_is_real_dir(entry):
                continue
            if _safe_is_real_dir(entry / "projects"):
                resolved = entry.resolve()
                if resolved not in seen:
                    found.append(entry)
                    seen.add(resolved)

    return found


def account_label(config_dir: Path, home: Path | None = None) -> str:
    """Human-readable label for a config dir: 'default' or 'account-N'."""
    home = home or default_home()
    if config_dir.resolve() == (home / ".claude").resolve():
        return "default"
    return config_dir.name


def current_config_dir(home: Path | None = None) -> Path:
    """The config dir for THIS process's own session, honoring $CLAUDE_CONFIG_DIR."""
    home = home or default_home()
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return home / ".claude"


def find_current_transcript(
    cwd: str | None = None, home: Path | None = None
) -> tuple[Path | None, str]:
    """Best-effort resolution of the CURRENT session's transcript file.

    Returns (path_or_None, how) where `how` explains the resolution strategy actually used
    — never silently guesses without saying so, since a subagent session (no direct env
    pointer to its own transcript file) has to fall back to "most recently modified".
    """
    home = home or default_home()
    cwd = cwd or os.getcwd()
    config_dir = current_config_dir(home)
    project_dir = config_dir / "projects" / slugify_cwd(cwd)

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    is_child = os.environ.get("CLAUDE_CODE_CHILD_SESSION") == "1"
    if session_id and not is_child and _SESSION_ID_RE.match(session_id):
        direct = project_dir / f"{session_id}.jsonl"
        if _safe_is_regular_file(direct):
            return direct, "resolved via $CLAUDE_CODE_SESSION_ID"

    if _safe_is_real_dir(project_dir):
        found = _most_recent_transcript(project_dir)
        if found is not None:
            reason = (
                "most recently modified transcript under the current project dir "
                "(session id env var did not resolve directly - likely a subagent session)"
            )
            return found, reason

    # Last resort: most recently modified transcript anywhere under this config dir.
    projects_root = config_dir / "projects"
    if _safe_is_real_dir(projects_root):
        found = _most_recent_transcript(projects_root)
        if found is not None:
            reason = (
                "most recently modified transcript across all projects "
                "(cwd-scoped lookup found nothing)"
            )
            return found, reason

    return None, "no transcript found"


def find_transcripts(config_dir: Path, modified_after: float | None = None) -> list[Path]:
    """All `.jsonl` transcripts under a config dir's `projects/`, recursively.

    `modified_after` (a unix timestamp), when given, skips files whose mtime is older -
    a cheap filter since a rolling rate-limit window means an untouched-in-N-hours file
    cannot contribute to the current window anyway.
    """
    projects_root = config_dir / "projects"
    if not _safe_is_real_dir(projects_root):
        return []
    out = []
    for path in _iter_jsonl_files(projects_root):
        if modified_after is not None:
            mtime = _safe_mtime(path)
            if mtime is None or mtime < modified_after:
                continue
        out.append(path)
    return out


def omp_sessions_root(home: Path | None = None) -> Path:
    """Oh My Pi parent-session root: ~/.omp/agent/sessions."""
    home = home or default_home()
    return home / ".omp" / "agent" / "sessions"


def find_parent_session_files(
    sessions_root: Path, modified_after: float | None = None
) -> list[Path]:
    """jsonl files whose parent dir is a direct child of `sessions_root`.

    Parent sessions live at `{sessions_root}/{slug}/{session}.jsonl`. Nested files
    (CC `subagents/`, OMP `{session}/{AgentName}.jsonl`) are not parent sessions.
    `modified_after` is an optional cheap mtime pre-filter; callers still filter
    by each message's own timestamp.
    """
    if not _safe_is_real_dir(sessions_root):
        return []
    try:
        slugs = list(sessions_root.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for slug_dir in slugs:
        if not _safe_is_real_dir(slug_dir):
            continue
        try:
            entries = list(slug_dir.iterdir())
        except OSError:
            continue
        for candidate in entries:
            if not candidate.name.endswith(".jsonl"):
                continue
            if not _safe_is_regular_file(candidate):
                continue
            if modified_after is not None:
                mtime = _safe_mtime(candidate)
                if mtime is None or mtime < modified_after:
                    continue
            out.append(candidate)
    return out
