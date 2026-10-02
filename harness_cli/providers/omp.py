"""omp's own tracked usage_history: a supplementary, already-recorded ground truth.

Accessed via: limits_collect.collect_all (appended after every network-probe
collector). omp (~/.omp/agent/agent.db) already polls provider usage on its
own schedule and records every poll to `usage_history`, keyed by
(provider, account_key, limit_id). This module reads that local sqlite
history read-only; it never calls a network endpoint itself.

Prior art / why read a local db instead of inventing a probe: ccusage (the
Claude Code usage CLI) parses local Claude Code JSONL session logs entirely
offline rather than hitting a network endpoint, on the premise that a
locally-recorded ground truth beats guessing when no clean API exists. This
module borrows that same heuristic, not ccusage's code (different language,
different scope — ccusage estimates token *cost* from transcripts; this
reads the provider's *official* usage-window percent that omp already
fetched and stored). It matters most for `providers/xai.py`, which
fail-closes with status="no_endpoint" because x.ai has no public
usage-percent API: omp's own `xai-oauth` rows are the only place that
percent exists anywhere on this machine, so collect_omp supplements (never
replaces) collect_xai's fail-closed stub.

Fails closed like every other provider: a missing db, missing table, or any
sqlite error yields `[]`, never an exception and never a guessed percent.

Test-injection convention: `HARNESS_LIMITS_OMP_DB` overrides the db path,
mirroring review-cli's own `OMP_AUTH_DB` override for the identical db
(reviewlib/backends.py) — same rationale: tests must never touch a real
`~/.omp/agent/agent.db` on the developer's or CI's machine.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.limits_model import LimitCard, LimitWindow

_DB_ENV = "HARNESS_LIMITS_OMP_DB"

# omp's own provider column -> the display name harness-cli's native
# collectors already use for that same underlying account, so `--json`
# output visually pairs an omp-sourced card with its native counterpart.
# opencode-go has no native collector (harness-cli tracks claude/codex/
# zai/xai/kimi, not opencode's own relay), so it gets a plain new name.
_CANONICAL_PROVIDER_NAMES: dict[str, str] = {
    "anthropic": "Claude Code",
    "kimi-code": "kimi",
    "xai-oauth": "x.ai",
    "zai": "z.ai",
    "opencode-go": "opencode",
}

# Every card sourced from omp's db is suffixed so it is never confused with
# a card produced by this repo's own live network probe for the same
# account (e.g. "x.ai" from providers/xai.py vs "x.ai (omp)" here).
_OMP_SUFFIX = " (omp)"

# omp's own account_key for a provider can churn across sessions (observed:
# kimi-code alone had 380+ distinct historical account_key values on a real
# machine, one born-and-abandoned per broken refresh attempt) leaving stale
# ghost rows in usage_history long after the credential rotated away. Only
# surface account_keys whose most recent recorded_at is within this window
# of that SAME provider's own most recent recorded_at — i.e. keep the
# provider's latest polling batch, drop old churn. A provider's genuinely
# distinct simultaneous accounts (e.g. four separate anthropic logins) are
# all polled together in one batch, so they all survive this filter.
_STALE_ACCOUNT_WINDOW_SECONDS = 24 * 3600

_FIVE_HOUR = 5 * 3600
_SEVEN_DAY = 7 * 86400


def _db_path(home: Path) -> Path:
    override = os.environ.get(_DB_ENV, "").strip()
    if override:
        return Path(override)
    return home / ".omp" / "agent" / "agent.db"


def _connect_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        conn.execute("SELECT 1 FROM usage_history LIMIT 1")
    except sqlite3.Error:
        return None
    return conn


def _infer_window_seconds(label: str, window_label: str | None) -> float | None:
    text = f"{label} {window_label or ''}".lower()
    if "total quo" in text:
        return _SEVEN_DAY
    if "7 day" in text or "weekly" in text or "week" in text:
        return _SEVEN_DAY
    if "5 hour" in text or "5h" in text:
        return _FIVE_HOUR
    return None


def _to_datetime(ms: object) -> datetime | None:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _account_label(account_key: str, email: str | None) -> str:
    if email:
        return email
    return account_key.rsplit("|", 1)[-1] or account_key


def _fetch_usage_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    query = """
        SELECT u.provider, u.account_key, u.email, u.label, u.window_label,
               u.used_fraction, u.status, u.resets_at, u.recorded_at
        FROM usage_history u
        JOIN (
            SELECT provider, account_key, label, MAX(id) AS max_id
            FROM usage_history
            GROUP BY provider, account_key, label
        ) latest
        ON u.provider = latest.provider
        AND u.account_key = latest.account_key
        AND u.label = latest.label
        AND u.id = latest.max_id
    """
    return list(conn.execute(query))


def _fetch_disabled_causes(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Every (provider, identity_key, disabled_cause) row with a live disable reason."""
    conn.row_factory = sqlite3.Row
    query = """
        SELECT provider, identity_key, disabled_cause
        FROM auth_credentials
        WHERE identity_key IS NOT NULL AND disabled_cause IS NOT NULL
    """
    try:
        rows = list(conn.execute(query))
    except sqlite3.Error:
        return []
    return [
        (row["provider"], row["identity_key"], row["disabled_cause"])
        for row in rows
        if row["provider"] and row["identity_key"] and row["disabled_cause"]
    ]


def _disabled_cause_for(
    provider: str, account_key: str, disabled: list[tuple[str, str, str]]
) -> str | None:
    for cred_provider, identity_key, cause in disabled:
        if cred_provider == provider and identity_key in account_key:
            return cause
    return None


def collect_omp(home: Path | None = None) -> list[LimitCard]:
    home = home or Path.home()
    conn = _connect_readonly(_db_path(home))
    if conn is None:
        return []
    try:
        rows = _fetch_usage_rows(conn)
        disabled = _fetch_disabled_causes(conn)
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    # provider -> account_key -> rows, preserving first-seen order for stable output.
    grouped: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for row in rows:
        provider = row["provider"]
        account_key = row["account_key"]
        grouped.setdefault(provider, {}).setdefault(account_key, []).append(row)

    cards: list[LimitCard] = []
    for provider, by_account in grouped.items():
        provider_latest = max(
            row["recorded_at"] for account_rows in by_account.values() for row in account_rows
        )
        for account_key, account_rows in by_account.items():
            account_latest = max(row["recorded_at"] for row in account_rows)
            if provider_latest - account_latest > _STALE_ACCOUNT_WINDOW_SECONDS * 1000:
                continue
            cards.append(_build_card(provider, account_key, account_rows, disabled))
    return cards


def _build_card(
    provider: str,
    account_key: str,
    rows: list[sqlite3.Row],
    disabled: list[tuple[str, str, str]],
) -> LimitCard:
    email = next((row["email"] for row in rows if row["email"]), None)
    windows = [
        LimitWindow(
            name=row["label"],
            used_percent=(row["used_fraction"] * 100) if row["used_fraction"] is not None else None,
            resets_at=_to_datetime(row["resets_at"]),
            window_seconds=_infer_window_seconds(row["label"], row["window_label"]),
        )
        for row in rows
    ]
    fetched_at = _to_datetime(max(row["recorded_at"] for row in rows))
    cause = _disabled_cause_for(provider, account_key, disabled)
    display_provider = _CANONICAL_PROVIDER_NAMES.get(provider, provider) + _OMP_SUFFIX
    return LimitCard(
        provider=display_provider,
        account=_account_label(account_key, email),
        windows=windows,
        fetched_at=fetched_at,
        status="auth_failed" if cause else "ok",
        reason=cause,
    )
