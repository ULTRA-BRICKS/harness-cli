"""Claude Code OAuth usage: five_hour / seven_day / seven_day_* windows.

Accessed via: limits_collect.collect_all/current → this module →
https://api.anthropic.com/api/oauth/usage.
Assumptions: utilization is only taken from that JSON. Missing creds, 401/403/429,
or a missing User-Agent are fail-closed, never filled from transcript tokens.
Past bugs: omitting User-Agent yields persistent 429s on this endpoint.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.limits_http import json_request
from harness_cli.limits_model import LimitCard, LimitWindow
from harness_cli.timeutil import parse_iso_timestamp

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_REFRESH_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_FIVE_HOUR = 5 * 3600
_SEVEN_DAY = 7 * 86400
_MISSING = "missing credentials — not guessed from tokens"


def window_label(key: str) -> str | None:
    if key == "five_hour":
        return "Session"
    if key == "seven_day":
        return "Weekly"
    if key.startswith("seven_day_"):
        suffix = key[len("seven_day_") :]
        if not suffix:
            return "Weekly"
        titled = suffix.replace("-", "_").replace(" ", "_")
        parts = [p for p in titled.split("_") if p]
        pretty = "".join(p[:1].upper() + p[1:] for p in parts)
        return f"Weekly·{pretty}" if pretty else "Weekly"
    return None


def window_seconds_for(key: str) -> float | None:
    if key == "five_hour":
        return float(_FIVE_HOUR)
    if key.startswith("seven_day"):
        return float(_SEVEN_DAY)
    return None


def _ordered_window_keys(payload: dict) -> list[str]:
    keys: list[str] = []
    if "five_hour" in payload:
        keys.append("five_hour")
    if "seven_day" in payload:
        keys.append("seven_day")
    extras = sorted(k for k in payload if isinstance(k, str) and k.startswith("seven_day_"))
    keys.extend(extras)
    return keys


def _finite_percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > 100:
        return None
    return float(value)


def _window_from_fields(
    name: str, percent: object, resets_raw: object, window_seconds: float | None
) -> LimitWindow | None:
    used = _finite_percent(percent)
    if used is None:
        return None
    resets_at = parse_iso_timestamp(resets_raw) if isinstance(resets_raw, str) else None
    return LimitWindow(
        name=name, used_percent=used, resets_at=resets_at, window_seconds=window_seconds
    )


def _windows_from_limits_array(payload: dict) -> list[LimitWindow]:
    """Screenshot windows live in `limits[]` (Session / Weekly / Weekly·Fable)."""
    raw = payload.get("limits")
    if not isinstance(raw, list):
        return []
    windows: list[LimitWindow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        percent = item.get("percent")
        resets_raw = item.get("resets_at")
        if kind == "session":
            win = _window_from_fields("Session", percent, resets_raw, _FIVE_HOUR)
        elif kind == "weekly_all":
            win = _window_from_fields("Weekly", percent, resets_raw, _SEVEN_DAY)
        elif kind == "weekly_scoped":
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            display = model.get("display_name")
            if not isinstance(display, str) or not display.strip():
                continue
            win = _window_from_fields(
                f"Weekly·{display.strip()}", percent, resets_raw, _SEVEN_DAY
            )
        else:
            continue
        if win is not None:
            windows.append(win)
    return windows


def parse_oauth_usage(payload: object) -> list[LimitWindow]:
    if not isinstance(payload, dict):
        return []
    from_limits = _windows_from_limits_array(payload)
    if from_limits:
        return from_limits
    windows: list[LimitWindow] = []
    for key in _ordered_window_keys(payload):
        raw = payload[key]
        if raw is None or not isinstance(raw, dict):
            continue
        label = window_label(key)
        if not label:
            continue
        win = _window_from_fields(
            label, raw.get("utilization"), raw.get("resets_at"), window_seconds_for(key)
        )
        if win is not None:
            windows.append(win)
    return windows


def format_plan(tier: object) -> str | None:
    if not isinstance(tier, str) or not tier:
        return None
    lowered = tier.lower()
    if "max" in lowered and "20" in lowered:
        return "Max 20x"
    if "max" in lowered:
        return "Max"
    return None


def _read_json(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def read_account_meta(config_dir: Path) -> tuple[str | None, str | None]:
    data = _read_json(config_dir / ".claude.json")
    # Stock Claude Code also writes ~/.claude.json (next to ~/.claude/), not
    # only ~/.claude/.claude.json — that sibling file is where the email lives
    # on this layout.
    if not data and config_dir.name == ".claude":
        data = _read_json(config_dir.parent / ".claude.json")
    data = data or {}
    oauth = data.get("oauthAccount") if isinstance(data.get("oauthAccount"), dict) else {}
    email = None
    if isinstance(oauth, dict):
        value = oauth.get("emailAddress")
        if isinstance(value, str) and value:
            email = value
    plan = format_plan(
        (oauth.get("organizationRateLimitTier") if isinstance(oauth, dict) else None)
        or data.get("organizationRateLimitTier")
    )
    return email, plan


def _oauth_from_mapping(data: dict) -> dict | None:
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = data
    token = oauth.get("accessToken") or oauth.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    return {
        "access_token": token,
        "refresh_token": oauth.get("refreshToken") or oauth.get("refresh_token"),
        "expires_at": oauth.get("expiresAt") or oauth.get("expires_at"),
        "plan": format_plan(oauth.get("rateLimitTier") or oauth.get("subscriptionType")),
    }


def read_file_credentials(config_dir: Path) -> dict | None:
    data = _read_json(config_dir / ".credentials.json")
    if not data:
        return None
    return _oauth_from_mapping(data)


def read_keychain_credentials() -> dict | None:
    if os.environ.get("HARNESS_LIMITS_KEYCHAIN", "1") == "0":
        return None
    try:
        import subprocess

        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _oauth_from_mapping(data)


def _env_credentials() -> dict | None:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        return None
    return {"access_token": token, "refresh_token": None, "expires_at": None}


def _token_expired(expires_at: object) -> bool:
    if expires_at is None:
        return False
    try:
        ts = float(expires_at)
    except (TypeError, ValueError):
        return False
    if ts > 1e12:
        ts /= 1000.0
    return ts <= time.time() + 60


def _detect_claude_version() -> str:
    override = os.environ.get("CLAUDE_CODE_VERSION")
    if isinstance(override, str) and override.strip():
        return override.strip().lstrip("v")
    versions_dir = Path.home() / ".local" / "share" / "claude" / "versions"
    try:
        names = [path.name for path in versions_dir.iterdir()]
    except OSError:
        names = []
    dotted = [name for name in names if name and name[0].isdigit()]
    if dotted:
        return max(dotted)
    return "2.1.263"


def _user_agent() -> str:
    return f"claude-code/{_detect_claude_version()}"


def _refresh_access_token(refresh_token: str) -> tuple[str | None, str | None]:
    """JSON refresh (Claude Code's own TOKEN_URL body), never form-urlencoded."""
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CLIENT_ID,
            "scope": (
                "user:profile user:inference user:sessions:claude_code "
                "user:mcp_servers user:file_upload"
            ),
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _user_agent(),
    }
    last_error = "token refresh failed"
    for url in _REFRESH_URLS:
        result = json_request(url, headers, method="POST", body=body)
        payload = result.payload
        if result.status == 200 and isinstance(payload, dict):
            token = payload.get("access_token")
            if isinstance(token, str) and token:
                return token, None
        if result.error:
            last_error = result.error
            if result.status == 429:
                return None, last_error
    return None, last_error


def _usage_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": _user_agent(),
        "Content-Type": "application/json",
    }


def fetch_usage(access_token: str) -> tuple[list[LimitWindow] | None, str | None]:
    result = json_request(USAGE_URL, _usage_headers(access_token))
    if result.error:
        return None, result.error
    windows = parse_oauth_usage(result.payload)
    if not windows:
        return None, "unrecognized usage payload"
    return windows, None


def _resolve_token(creds: dict) -> tuple[str | None, str | None]:
    token = creds.get("access_token")
    refresh = creds.get("refresh_token")
    if _token_expired(creds.get("expires_at")) and isinstance(refresh, str) and refresh:
        refreshed, err = _refresh_access_token(refresh)
        if refreshed:
            return refreshed, None
        return None, err or "token expired — refresh failed"
    if isinstance(token, str) and token:
        return token, None
    return None, _MISSING


def iter_config_dirs(home: Path) -> list[Path]:
    from harness_cli.discovery import discover_account_dirs

    return discover_account_dirs(home)


def current_config_dir(home: Path) -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return home / ".claude"


def _fail_card(account: str, plan: str | None, reason: str, status: str) -> LimitCard:
    return LimitCard(
        provider="Claude Code",
        account=account,
        plan=plan,
        windows=[],
        fetched_at=datetime.now(timezone.utc),
        status=status,
        reason=reason,
    )


def _live_card(account: str, plan: str | None, windows: list[LimitWindow]) -> LimitCard:
    return LimitCard(
        provider="Claude Code",
        account=account,
        plan=plan,
        windows=windows,
        fetched_at=datetime.now(timezone.utc),
        status="ok",
    )


def _pick_freshest_creds(*candidates: dict | None) -> dict | None:
    """Prefer an unexpired token; on macOS the Keychain copy is the live one."""
    unexpired = [c for c in candidates if c and not _token_expired(c.get("expires_at"))]
    if unexpired:
        return unexpired[0]
    for candidate in candidates:
        if candidate:
            return candidate
    return None


def _creds_for_slot(config_dir: Path, home: Path) -> dict | None:
    file_creds = read_file_credentials(config_dir)
    keyed = None
    env_creds = None
    try:
        is_current = config_dir.resolve() == current_config_dir(home).resolve()
    except OSError:
        is_current = False
    if is_current:
        keyed = read_keychain_credentials()
        env_creds = _env_credentials()
    # Keychain first: ~/.claude/.credentials.json is often stale while Keychain is live.
    return _pick_freshest_creds(keyed, file_creds, env_creds)


def _group_slots(home: Path) -> list[tuple[str, str | None, Path, dict | None]]:
    """One row per distinct email/slot: (account, plan, dir, creds)."""
    grouped: dict[str, tuple[str | None, Path, dict | None]] = {}
    order: list[str] = []
    for config_dir in iter_config_dirs(home):
        email, plan = read_account_meta(config_dir)
        creds = _creds_for_slot(config_dir, home)
        key = email or f"slot:{config_dir}"
        account = email or config_dir.name
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = (plan, config_dir, creds)
            order.append(key)
            continue
        # Prefer a slot that actually has credentials so the same live oauth
        # is not shown once live and again as missing-creds.
        if existing[2] is None and creds is not None:
            grouped[key] = (plan or existing[0], config_dir, creds)
        elif plan and not existing[0]:
            grouped[key] = (plan, existing[1], existing[2])
    rows = []
    for key in order:
        plan, config_dir, creds = grouped[key]
        if key.startswith("slot:"):
            email, meta_plan = read_account_meta(config_dir)
            account = email or ("default" if config_dir.name == ".claude" else config_dir.name)
            plan = plan or meta_plan
        else:
            account = key
        if (not plan) and creds:
            plan = creds.get("plan") if isinstance(creds.get("plan"), str) else plan
        rows.append((account, plan, config_dir, creds))
    return rows


def collect_claude(home: Path | None = None, *, current_only: bool = False) -> list[LimitCard]:
    home = home or Path.home()
    slots = _group_slots(home)
    if current_only:
        current = current_config_dir(home).resolve()
        current_email, _ = read_account_meta(current) if current.is_dir() else (None, None)
        filtered = []
        for account, plan, config_dir, creds in slots:
            if config_dir.resolve() == current or (current_email and account == current_email):
                filtered.append((account, plan, config_dir, creds))
        slots = filtered or (
            [(current_email or current.name, None, current, _creds_for_slot(current, home))]
            if current.is_dir()
            else []
        )
    cards: list[LimitCard] = []
    for account, plan, _config_dir, creds in slots:
        if not creds:
            cards.append(_fail_card(account, plan, _MISSING, "missing_creds"))
            continue
        plan = plan or (creds.get("plan") if isinstance(creds.get("plan"), str) else None)
        token, err = _resolve_token(creds)
        if not token:
            status = "http_error" if err and "HTTP" in (err or "") else "missing_creds"
            cards.append(_fail_card(account, plan, err or _MISSING, status))
            continue
        windows, err = fetch_usage(token)
        refresh = creds.get("refresh_token")
        if windows is None and err and "401" in err and isinstance(refresh, str) and refresh:
            refreshed, rerr = _refresh_access_token(refresh)
            if refreshed:
                windows, err = fetch_usage(refreshed)
            else:
                err = rerr or err
        if windows is None:
            status = "http_error"
            cards.append(_fail_card(account, plan, err or "unavailable", status))
            continue
        cards.append(_live_card(account, plan, windows))
    return cards
