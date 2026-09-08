"""x.ai / SuperGrok: no public usage-percent API — fail-closed.

Accessed via: limits_collect.collect_all. Auth may exist in OpenCode auth.json
under `xai`, but there is no documented % usage endpoint. Do not invent a
percent from local token logs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.limits_model import LimitCard

_NO_ENDPOINT = "no usage endpoint — not guessed from tokens"


def _account_label(home: Path) -> str:
    xdg = os.environ.get("XDG_DATA_HOME")
    paths = []
    if xdg:
        paths.append(Path(xdg) / "opencode" / "auth.json")
    paths.append(home / ".local" / "share" / "opencode" / "auth.json")
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        entry = data.get("xai") or data.get("x-ai") or data.get("x.ai")
        if entry:
            if isinstance(entry, dict):
                email = entry.get("email") or entry.get("account")
                if isinstance(email, str) and email:
                    return email
            return "x.ai"
    return "x.ai"


def collect_xai(home: Path | None = None) -> LimitCard:
    home = home or Path.home()
    return LimitCard(
        provider="x.ai",
        account=_account_label(home),
        fetched_at=datetime.now(timezone.utc),
        status="no_endpoint",
        reason=_NO_ENDPOINT,
    )
