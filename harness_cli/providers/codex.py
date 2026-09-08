"""Codex/ChatGPT usage via GET chatgpt.com/backend-api/wham/usage.

Accessed via: limits_collect.collect_all. Credentials from ~/.codex/auth.json.
Assumptions: percent comes from used_percent (or equivalent) on that payload.
Never POST reset-credits/consume. Missing creds / 401/403/429 fail-closed.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.limits_http import json_request
from harness_cli.limits_model import LimitCard, LimitWindow
from harness_cli.timeutil import parse_iso_timestamp

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_MISSING = "missing credentials — not guessed from tokens"
_FIVE_HOUR = 5 * 3600
_SEVEN_DAY = 7 * 86400


def _read_auth(home: Path) -> tuple[str | None, str | None]:
    path = home / ".codex" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    access_s = access if isinstance(access, str) and access else None
    account_s = account_id if isinstance(account_id, str) and account_id else None
    return access_s, account_s


def _as_percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pct = float(value)
    if not math.isfinite(pct):
        return None
    if 0 <= pct <= 1:
        pct *= 100.0
    if pct < 0 or pct > 100:
        return None
    return pct


def _as_resets(obj: dict) -> datetime | None:
    for key in ("resets_at", "reset_at", "resetAt", "resetsAt"):
        raw = obj.get(key)
        if isinstance(raw, str):
            parsed = parse_iso_timestamp(raw)
            if parsed:
                return parsed
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return None
    after = obj.get("reset_after_seconds") or obj.get("reset_after")
    if isinstance(after, (int, float)) and not isinstance(after, bool) and after >= 0:
        return datetime.now(timezone.utc).timestamp() and datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + float(after), tz=timezone.utc
        )
    return None


def _window_seconds(obj: dict) -> float | None:
    for key in ("window_seconds", "limit_window_seconds"):
        raw = obj.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            return float(raw)
    return None


def _name_for_window(fallback: str, obj: dict) -> str:
    seconds = _window_seconds(obj)
    if seconds is not None:
        if seconds <= 6 * 3600:
            return "Session"
        if seconds >= 2 * 86400:
            return "Weekly"
    return fallback


def _add_window(windows: list[LimitWindow], name: str, obj: object) -> None:
    if not isinstance(obj, dict):
        return
    pct = None
    for key in ("used_percent", "usedPercent", "utilization"):
        pct = _as_percent(obj.get(key))
        if pct is not None:
            break
    if pct is None:
        return
    seconds = _window_seconds(obj)
    label = _name_for_window(name, obj)
    if seconds is None:
        seconds = float(_FIVE_HOUR) if label == "Session" else float(_SEVEN_DAY)
    windows.append(
        LimitWindow(
            name=label,
            used_percent=pct,
            resets_at=_as_resets(obj),
            window_seconds=seconds,
        )
    )


def parse_codex_usage(payload: object) -> list[LimitWindow]:
    if not isinstance(payload, dict):
        return []
    windows: list[LimitWindow] = []
    rate = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else payload
    named = (
        ("Session", "primary_window"),
        ("Weekly", "secondary_window"),
        ("Session", "five_hour"),
        ("Weekly", "seven_day"),
        ("Session", "primary"),
        ("Weekly", "secondary"),
    )
    if isinstance(rate, dict):
        for name, key in named:
            if key in rate:
                _add_window(windows, name, rate[key])
        if not windows:
            _add_window(windows, "Session", rate)
    if not windows:
        _add_window(windows, "Session", payload)
    return windows



def _ok_card(account: str, payload: object, windows: list[LimitWindow], now: datetime) -> LimitCard:
    email = payload.get("email") if isinstance(payload, dict) else None
    if isinstance(email, str) and email:
        account = email
    plan = payload.get("plan_type") if isinstance(payload, dict) else None
    plan_s = plan.capitalize() if isinstance(plan, str) and plan else None
    return LimitCard(
        provider="Codex",
        account=account,
        plan=plan_s,
        windows=windows,
        fetched_at=now,
        status="ok",
    )


def collect_codex(home: Path | None = None) -> LimitCard:
    home = home or Path.home()
    now = datetime.now(timezone.utc)
    access, account_id = _read_auth(home)
    account = account_id or "codex"
    if not access:
        return LimitCard(
            provider="Codex",
            account=account,
            fetched_at=now,
            status="missing_creds",
            reason=_MISSING,
        )
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    result = json_request(USAGE_URL, headers)
    if result.error:
        return LimitCard(
            provider="Codex",
            account=account,
            fetched_at=now,
            status="http_error",
            reason=result.error,
        )
    windows = parse_codex_usage(result.payload)
    if not windows:
        return LimitCard(
            provider="Codex",
            account=account,
            fetched_at=now,
            status="http_error",
            reason="unrecognized usage payload",
        )
    return _ok_card(account, result.payload, windows, now)
