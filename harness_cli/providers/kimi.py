"""kimi-for-coding usages: GET api.kimi.com/coding/v1/usages.

Accessed via: limits_collect.collect_all. Key from OpenCode auth.json
(`kimi-for-coding` / `moonshotai`), Bearer sk-kimi-… (coding key, not platform).
Percent = 100 * (limit - remaining) / limit from usage.limit/remaining/resetTime.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.limits_http import json_request
from harness_cli.limits_model import LimitCard, LimitWindow
from harness_cli.timeutil import parse_iso_timestamp

USAGE_URL = "https://api.kimi.com/coding/v1/usages"
_MISSING = "missing credentials — not guessed from tokens"
_PROVIDER_IDS = ("kimi-for-coding", "kimi", "moonshotai", "moonshot")
_SEVEN_DAY = 7 * 86400


def _auth_paths(home: Path) -> list[Path]:
    paths = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        paths.append(Path(xdg) / "opencode" / "auth.json")
    paths.append(home / ".local" / "share" / "opencode" / "auth.json")
    return paths


def _key_from_entry(entry: object) -> str | None:
    if isinstance(entry, str) and entry:
        return entry
    if not isinstance(entry, dict):
        return None
    for key in ("apiKey", "api_key", "key", "token", "accessToken"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def read_kimi_key(home: Path) -> str | None:
    for path in _auth_paths(home):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for provider_id in _PROVIDER_IDS:
            key = _key_from_entry(data.get(provider_id))
            if key:
                return key
    return None


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _percent_from_usage(usage: dict) -> float | None:
    limit = _as_number(usage.get("limit"))
    remaining = _as_number(usage.get("remaining"))
    if limit is None or remaining is None or limit <= 0:
        return None
    pct = 100.0 * (limit - remaining) / limit
    if not math.isfinite(pct):
        return None
    if pct < 0:
        pct = 0.0
    if pct > 100:
        pct = 100.0
    return pct


def _resets_at(usage: dict) -> datetime | None:
    raw = usage.get("resetTime") or usage.get("reset_time") or usage.get("resets_at")
    if isinstance(raw, str):
        return parse_iso_timestamp(raw)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def parse_kimi_usages(payload: object) -> list[LimitWindow]:
    if not isinstance(payload, dict):
        return []
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    if not isinstance(usage, dict):
        return []
    pct = _percent_from_usage(usage)
    if pct is None:
        return []
    return [
        LimitWindow(
            name="Weekly",
            used_percent=pct,
            resets_at=_resets_at(usage),
            window_seconds=float(_SEVEN_DAY),
        )
    ]


def collect_kimi(home: Path | None = None) -> LimitCard:
    home = home or Path.home()
    now = datetime.now(timezone.utc)
    key = read_kimi_key(home)
    if not key:
        return LimitCard(
            provider="kimi",
            account="kimi",
            fetched_at=now,
            status="missing_creds",
            reason=_MISSING,
        )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    result = json_request(USAGE_URL, headers)
    if result.error:
        return LimitCard(
            provider="kimi",
            account="kimi",
            fetched_at=now,
            status="http_error",
            reason=result.error,
        )
    windows = parse_kimi_usages(result.payload)
    if not windows:
        return LimitCard(
            provider="kimi",
            account="kimi",
            fetched_at=now,
            status="http_error",
            reason="unrecognized usage payload",
        )
    return LimitCard(
        provider="kimi",
        account="kimi",
        windows=windows,
        fetched_at=now,
        status="ok",
    )
