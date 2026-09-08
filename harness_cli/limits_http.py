"""stdlib urllib JSON GET/POST with timeouts.

Accessed via: provider collectors (claude/codex/zai/kimi) when `harness limits` runs.
Assumptions: callers never log the Request, headers, or body — Authorization and
tokens must not appear in errors, stdout, or traces. 401/403/429 are returned as
fail-closed results, not raised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request
import urllib.request

_DEFAULT_TIMEOUT = 10
_MAX_BODY = 1_000_000


@dataclass
class HttpResult:
    status: int
    payload: Any = None
    error: str | None = None


def _reason_for_status(status: int) -> str:
    if status == 401:
        return "unauthorized (HTTP 401)"
    if status == 403:
        return "forbidden (HTTP 403)"
    if status == 429:
        return "rate limited (HTTP 429)"
    if status >= 500:
        return f"server error (HTTP {status})"
    if status <= 0:
        return "network error"
    return f"HTTP {status}"


def _parse_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _headers_ok(headers: dict[str, str]) -> bool:
    for value in headers.values():
        if not isinstance(value, str):
            return False
        if any(ord(ch) > 255 or ch in "\r\n" for ch in value):
            return False
    return True


def _read_bounded(fp, limit: int = _MAX_BODY) -> bytes | None:
    try:
        raw = fp.read(limit + 1)
    except OSError:
        return b""
    if raw is None:
        return b""
    if len(raw) > limit:
        return None
    return raw


def json_request(
    url: str,
    headers: dict[str, str],
    *,
    method: str = "GET",
    body: bytes | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> HttpResult:
    """GET/POST JSON. Never includes header values in the returned error."""
    if not _headers_ok(headers):
        return HttpResult(status=0, payload=None, error="invalid credentials")
    request = Request(url, data=body, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = _read_bounded(resp)
            status = int(getattr(resp, "status", None) or resp.getcode() or 0)
    except HTTPError as exc:
        try:
            code = int(exc.code)
            raw = b""
            if exc.fp is not None:
                bounded = _read_bounded(exc.fp)
                raw = b"" if bounded is None else bounded
            return HttpResult(status=code, payload=_parse_body(raw), error=_reason_for_status(code))
        except Exception:
            return HttpResult(status=0, payload=None, error="network error")
    except TimeoutError:
        return HttpResult(status=0, payload=None, error="timeout")
    except URLError:
        return HttpResult(status=0, payload=None, error="network error")
    except (OSError, ValueError, UnicodeError):
        # ValueError/UnicodeError: malformed header values. Never stringify —
        # urllib's message can include the Authorization header.
        return HttpResult(status=0, payload=None, error="network error")
    except Exception:
        return HttpResult(status=0, payload=None, error="network error")

    if raw is None:
        return HttpResult(status=0, payload=None, error="unrecognized usage payload")
    if status != 200:
        return HttpResult(status=status, payload=_parse_body(raw), error=_reason_for_status(status))
    payload = _parse_body(raw)
    if payload is None and raw:
        return HttpResult(status=status, payload=None, error="unrecognized usage payload")
    return HttpResult(status=status, payload=payload, error=None)
