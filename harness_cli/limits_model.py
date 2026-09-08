"""Rate-limit proximity cards and windows.

Accessed via: `harness limits --all|--current` → limits_collect → limits_render.
Assumptions: used_percent is set only from a provider usage API, never from
local transcript token counts. A missing credential or endpoint yields a
fail-closed card (status != ok, windows empty).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class LimitWindow:
    """One real usage window (Session / Weekly / Weekly·Fable / ...)."""

    name: str
    used_percent: float | None = None
    resets_at: datetime | None = None
    window_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "used_percent": self.used_percent,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "window_seconds": self.window_seconds,
        }


@dataclass
class LimitCard:
    """One account/provider card in the proximity TUI / JSON."""

    provider: str
    account: str
    plan: str | None = None
    windows: list[LimitWindow] = field(default_factory=list)
    fetched_at: datetime | None = None
    status: str = "ok"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account": self.account,
            "plan": self.plan,
            "status": self.status,
            "reason": self.reason,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "windows": [w.to_dict() for w in self.windows],
        }
