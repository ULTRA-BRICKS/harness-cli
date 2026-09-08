"""z.ai / GLM coding-plan quota: GET /api/monitor/usage/quota/limit.

Accessed via: limits_collect.collect_all. Auth from OpenCode auth.json (zai /
zai-coding-plan / zhipu). Authorization is the RAW API key — no Bearer prefix
(see guyinwonder168/opencode-glm-quota). Percent comes from limits[].percentage.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.limits_http import json_request
from harness_cli.limits_model import LimitCard, LimitWindow

USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_MISSING = "missing credentials — not guessed from tokens"
_PROVIDER_IDS = ("zai-coding-plan", "zai", "z-ai", "z.ai", "zhipu", "zhipuai")
_FIVE_HOUR = 5 * 3600
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
    for key in ("apiKey", "api_key", "key", "token", "accessToken", "auth_token"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def read_zai_key(home: Path) -> str | None:
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
    env = os.environ.get("ZAI_API_KEY") or os.environ.get("ZHIPU_API_KEY")
    return env if env else None


def _percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pct = float(value)
    if not math.isfinite(pct):
        return None
    if pct < 0 or pct > 100:
        return None
    return pct


def _resets_at(raw: object) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    ts = float(raw)
    if ts > 1e12:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _window_name(item: dict) -> str | None:
    kind = item.get("type")
    unit = item.get("unit")
    number = item.get("number")
    if kind == "TIME_LIMIT":
        return None
    if kind == "TOKENS_LIMIT" or kind == "Token usage(5 Hour)" or (unit == 3 and number == 5):
        if unit == 6 and number == 1:
            return "Weekly"
        if unit == 3 and number == 5:
            return "Session"
        if kind == "Token usage(Weekly)":
            return "Weekly"
        if kind == "Token usage(5 Hour)":
            return "Session"
    if kind == "Token usage(Weekly)" or (unit == 6 and number == 1):
        return "Weekly"
    if isinstance(kind, str) and "week" in kind.lower():
        return "Weekly"
    if isinstance(kind, str) and ("5" in kind or "hour" in kind.lower() or "session" in kind.lower()):
        return "Session"
    return None


def parse_zai_quota(payload: object) -> list[LimitWindow]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        return []
    windows: list[LimitWindow] = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        name = _window_name(item)
        if not name:
            continue
        pct = _percent(item.get("percentage"))
        if pct is None:
            continue
        seconds = float(_FIVE_HOUR) if name == "Session" else float(_SEVEN_DAY)
        windows.append(
            LimitWindow(
                name=name,
                used_percent=pct,
                resets_at=_resets_at(item.get("nextResetTime")),
                window_seconds=seconds,
            )
        )
    return windows


def collect_zai(home: Path | None = None) -> LimitCard:
    home = home or Path.home()
    now = datetime.now(timezone.utc)
    key = read_zai_key(home)
    if not key:
        return LimitCard(
            provider="z.ai",
            account="z.ai",
            fetched_at=now,
            status="missing_creds",
            reason=_MISSING,
        )
    headers = {
        "Authorization": key,
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
    }
    result = json_request(USAGE_URL, headers)
    if result.error:
        return LimitCard(
            provider="z.ai",
            account="z.ai",
            fetched_at=now,
            status="http_error",
            reason=result.error,
        )
    windows = parse_zai_quota(result.payload)
    if not windows:
        return LimitCard(
            provider="z.ai",
            account="z.ai",
            fetched_at=now,
            status="http_error",
            reason="unrecognized usage payload",
        )
    return LimitCard(
        provider="z.ai",
        account="z.ai",
        windows=windows,
        fetched_at=now,
        status="ok",
    )
