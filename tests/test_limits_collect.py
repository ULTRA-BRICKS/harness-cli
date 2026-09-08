"""Collector fail-closed contract: missing creds / HTTP 401 never become a fake %."""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

from harness_cli.limits_collect import collect_all
from harness_cli.providers import claude as claude_provider


def _isolate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_LIMITS_KEYCHAIN", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)


def _write_claude_account(root: Path, email: str, *, creds: dict | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": email},
                "organizationRateLimitTier": "default_claude_max_20x",
            }
        ),
        encoding="utf-8",
    )
    (root / "projects").mkdir(exist_ok=True)
    if creds is not None:
        (root / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")


def test_missing_creds_fail_closed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _write_claude_account(tmp_path / ".claude", "invntrm@gmail.com", creds=None)

    cards = collect_all()
    claude_cards = [c for c in cards if c.provider == "Claude Code"]
    assert claude_cards, "expected a fail-closed Claude card"
    card = claude_cards[0]
    assert card.status == "missing_creds"
    assert card.windows == []
    assert card.reason
    assert "not guessed" in card.reason.lower() or "missing" in card.reason.lower()
    assert all(w.used_percent is None for w in card.windows)


def test_http_401_fail_closed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _write_claude_account(
        tmp_path / ".claude",
        "invntrm@gmail.com",
        creds={
            "claudeAiOauth": {
                "accessToken": "access-not-for-log",
                "refreshToken": "refresh-not-for-log",
                "expiresAt": 4102444800000,
            }
        },
    )

    def boom(*args, **kwargs):
        raise HTTPError(
            "https://api.anthropic.com/api/oauth/usage",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr("urllib.request.urlopen", boom)

    cards = collect_all()
    claude_cards = [c for c in cards if c.provider == "Claude Code"]
    assert claude_cards
    card = claude_cards[0]
    assert card.status != "ok"
    assert card.windows == [] or all(w.used_percent is None for w in card.windows)
    assert card.reason
    assert "401" in card.reason


def test_never_fills_utilization_from_token_counts(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    account = tmp_path / ".claude"
    _write_claude_account(account, "invntrm@gmail.com", creds=None)
    transcript = account / "projects" / "proj" / "sess.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-08T11:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 999999,
                        "output_tokens": 888888,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cards = collect_all()
    for card in cards:
        for window in card.windows:
            assert window.used_percent is None
        if card.status == "ok":
            raise AssertionError("collector must not invent an ok card from transcripts")


def test_claude_keychain_helper_disabled_in_tests(monkeypatch):
    monkeypatch.setenv("HARNESS_LIMITS_KEYCHAIN", "0")
    assert claude_provider.read_keychain_credentials() is None


def test_urlopen_valueerror_fail_closed_without_leaking(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _write_claude_account(
        tmp_path / ".claude",
        "invntrm@gmail.com",
        creds={
            "claudeAiOauth": {
                "accessToken": "access-not-for-log",
                "refreshToken": "refresh-not-for-log",
                "expiresAt": 4102444800000,
            }
        },
    )

    def boom(*args, **kwargs):
        raise ValueError("header Authorization: Bearer access-not-for-log")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    cards = collect_all()
    claude_cards = [c for c in cards if c.provider == "Claude Code"]
    assert claude_cards
    card = claude_cards[0]
    assert card.status != "ok"
    blob = (card.reason or "") + card.account
    assert "access-not-for-log" not in blob
    assert "Bearer" not in blob


def test_reads_email_from_sibling_dot_claude_json(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "projects").mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "invntrm@gmail.com",
                    "organizationRateLimitTier": "default_claude_max_20x",
                }
            }
        ),
        encoding="utf-8",
    )
    cards = collect_all()
    claude_cards = [c for c in cards if c.provider == "Claude Code"]
    assert any(c.account == "invntrm@gmail.com" for c in claude_cards)
    assert all(c.account != ".claude" and c.account != "default" for c in claude_cards)
