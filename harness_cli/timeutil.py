"""Small timestamp helper. Stdlib-only (datetime)."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse a Claude Code transcript timestamp ("...Z" ISO-8601) into an aware datetime.

    Returns None for anything that isn't a well-formed timestamp - never raises, since a
    malformed or absent timestamp should just be treated as "unknown", not crash a scan.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
