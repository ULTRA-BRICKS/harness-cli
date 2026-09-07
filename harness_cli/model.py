"""Data model for observed harness usage. Stdlib-only (dataclasses)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Keyword -> category, checked in order against the lowercased error text. Order matters:
# more specific phrases are checked before generic ones so "session limit" doesn't get
# swallowed by a bare "limit" match. Phrases below are taken VERBATIM from real
# isApiErrorMessage text observed across this machine's own transcripts (~136 events,
# 2026-08 to 2026-09) - not guessed:
#   "You've hit your session limit · resets 7:30pm (Europe/Belgrade)"
#   "You've hit your weekly limit · resets 8pm (Europe/Belgrade)"
#   "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models..."
#   "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
#   "API Error: Fable 5's safeguards flagged this message ..."
#   "Not logged in · Please run /login"
ERROR_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("weekly limit", "weekly_limit"),
    ("session limit", "session_limit"),
    ("not your usage limit", "rate_limit"),  # explicit server-overload wording, checked
    ("reached your", "model_limit"),  # before the generic "limit" match below
    ("safeguards flagged", "safety_flag"),
    ("usage limit", "session_limit"),
    ("rate limit", "rate_limit"),
    ("429", "rate_limit"),
    ("overloaded", "overloaded"),
    ("529", "overloaded"),
    ("not logged in", "auth"),
    ("please run /login", "auth"),
    ("invalid api key", "auth"),
    ("authentication", "auth"),
)

# "You've hit your session limit · resets 7:30pm (Europe/Belgrade)" -> "7:30pm (Europe/Belgrade)"
_RESET_HINT_RE = re.compile(r"resets\s+(.+)$", re.IGNORECASE)


def classify_error_text(text: str) -> str:
    """Classify an observed API-error message's category from its text.

    Returns one of the ERROR_CATEGORY_KEYWORDS categories, or "other" when the text
    doesn't match anything known. Never fabricates a category the text doesn't support.
    """
    lowered = text.lower()
    for phrase, category in ERROR_CATEGORY_KEYWORDS:
        if phrase in lowered:
            return category
    return "other"


def extract_reset_hint(text: str) -> str | None:
    """Pull the raw "resets <local time> (<timezone>)" hint out of an error message, if any.

    This is the exact wall-clock text Claude Code itself included - NOT parsed into an
    absolute timestamp, because the message never states the date, so "resets 7:30pm" could
    mean today or tomorrow; presenting it as an absolute epoch would fabricate precision
    that isn't there. Returns None when the text carries no such hint.
    """
    match = _RESET_HINT_RE.search(text)
    return match.group(1).strip() if match else None


# No real Claude Code session has ever used anywhere close to this many tokens - this
# exists purely as a sanity ceiling against a malicious/corrupted transcript value with
# an absurd digit count. Python ints are arbitrary-precision (no crash on the ARITHMETIC
# itself), but formatting a sum of two such huge-but-individually-"valid" values back to
# a string for display can still exceed `sys.get_int_max_str_digits()` and raise
# ValueError at PRINT time, even though each field parsed fine on its own.
_MAX_SANE_TOKEN_COUNT = 10**12


def _as_token_count(value: object) -> int:
    """Coerce one `usage` field to a real token count, or 0 - never fabricate, never crash.

    A malformed transcript line must not crash the scan NOR silently contribute a
    fabricated count. Rejected as "not a real token count" (-> 0): bool (`int(True)` is 1,
    which would misreport a boolean as one real token), NaN/Infinity (`int()` raises on
    these - Python's `json.loads` accepts them even though standard JSON doesn't), a
    non-integral float (a real token count is always a whole number), a negative value (a
    token count can't be negative), and anything past _MAX_SANE_TOKEN_COUNT. Only a
    genuine, sane, non-negative integral int/float is trusted.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_SANE_TOKEN_COUNT else 0
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value < 0:
            return 0
        if value > _MAX_SANE_TOKEN_COUNT:
            return 0
        return int(value)
    return 0


@dataclass
class UsageTotals:
    """Sum of Claude API `usage` fields observed across a set of transcript lines.

    These are the exact fields Claude Code itself writes per assistant turn (input_tokens,
    output_tokens, cache_creation_input_tokens, cache_read_input_tokens) - not an estimate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    assistant_turns: int = 0

    def add(self, usage: dict) -> None:
        self.input_tokens += _as_token_count(usage.get("input_tokens"))
        self.output_tokens += _as_token_count(usage.get("output_tokens"))
        self.cache_creation_input_tokens += _as_token_count(
            usage.get("cache_creation_input_tokens")
        )
        self.cache_read_input_tokens += _as_token_count(usage.get("cache_read_input_tokens"))
        self.assistant_turns += 1

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class ApiErrorEvent:
    """One observed `isApiErrorMessage: true` transcript line."""

    timestamp: str
    category: str
    text: str
    reset_hint: str | None = None


@dataclass
class SessionUsage:
    """Observed usage for one transcript (`.jsonl`) file."""

    transcript_path: str
    account: str
    project_dir: str
    session_id: str
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    message_count: int = 0
    models: set[str] = field(default_factory=set)
    totals: UsageTotals = field(default_factory=UsageTotals)
    error_events: list[ApiErrorEvent] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "transcript_path": self.transcript_path,
            "account": self.account,
            "project_dir": self.project_dir,
            "session_id": self.session_id,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "message_count": self.message_count,
            "models": sorted(self.models),
            "usage": {
                "input_tokens": self.totals.input_tokens,
                "output_tokens": self.totals.output_tokens,
                "cache_creation_input_tokens": self.totals.cache_creation_input_tokens,
                "cache_read_input_tokens": self.totals.cache_read_input_tokens,
                "total_tokens": self.totals.total_tokens,
                "assistant_turns": self.totals.assistant_turns,
            },
            "error_events": [
                {
                    "timestamp": e.timestamp,
                    "category": e.category,
                    "text": e.text,
                    "reset_hint": e.reset_hint,
                }
                for e in self.error_events
            ],
        }
