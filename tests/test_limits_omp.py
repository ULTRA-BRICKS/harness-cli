"""omp-db-sourced provider cards: real, timestamped, per-account usage from omp's own
`usage_history`/`auth_credentials` tables. Fail-closed contract mirrors every other
provider: missing db / missing table / any sqlite error -> `[]`, never a raised exception.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from harness_cli.limits_collect import collect_all
from harness_cli.providers.omp import collect_omp


def _isolate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_LIMITS_KEYCHAIN", "0")
    monkeypatch.delenv("HARNESS_LIMITS_OMP_DB", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE usage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at INTEGER NOT NULL,
            provider TEXT NOT NULL,
            account_key TEXT NOT NULL,
            email TEXT,
            account_id TEXT,
            limit_id TEXT NOT NULL,
            label TEXT NOT NULL,
            window_label TEXT,
            used_fraction REAL,
            status TEXT,
            resets_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE auth_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            data TEXT NOT NULL,
            disabled_cause TEXT DEFAULT NULL,
            identity_key TEXT DEFAULT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    return conn


def _insert_usage(
    conn: sqlite3.Connection,
    *,
    provider: str,
    account_key: str,
    email: str | None,
    limit_id: str,
    label: str,
    window_label: str | None,
    used_fraction: float | None,
    status: str,
    resets_at: int | None,
    recorded_at: int,
) -> None:
    conn.execute(
        """
        INSERT INTO usage_history
            (recorded_at, provider, account_key, email, limit_id, label, window_label,
             used_fraction, status, resets_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recorded_at,
            provider,
            account_key,
            email,
            limit_id,
            label,
            window_label,
            used_fraction,
            status,
            resets_at,
        ),
    )


def _insert_credential(
    conn: sqlite3.Connection,
    *,
    provider: str,
    identity_key: str | None,
    disabled_cause: str | None,
) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO auth_credentials
            (provider, credential_type, data, disabled_cause, identity_key, created_at, updated_at)
        VALUES (?, 'oauth', '{}', ?, ?, ?, ?)
        """,
        (provider, disabled_cause, identity_key, now, now),
    )


def test_fresh_ok_and_exhausted_windows(tmp_path, monkeypatch):
    now_ms = int(time.time() * 1000)
    db_path = tmp_path / "agent.db"
    conn = _make_db(db_path)
    _insert_usage(
        conn,
        provider="anthropic",
        account_key="oauth|account:abc|email:a@x.com",
        email="a@x.com",
        limit_id="anthropic:5h",
        label="Claude 5 Hour",
        window_label="5 Hour",
        used_fraction=0.1,
        status="ok",
        resets_at=now_ms + 3600_000,
        recorded_at=now_ms,
    )
    _insert_usage(
        conn,
        provider="anthropic",
        account_key="oauth|account:abc|email:a@x.com",
        email="a@x.com",
        limit_id="anthropic:7d",
        label="Claude 7 Day",
        window_label="7 Day",
        used_fraction=1.0,
        status="exhausted",
        resets_at=now_ms + 86_400_000,
        recorded_at=now_ms,
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HARNESS_LIMITS_OMP_DB", str(db_path))
    cards = collect_omp(tmp_path)

    assert len(cards) == 1
    card = cards[0]
    assert card.provider == "Claude Code (omp)"
    assert card.account == "a@x.com"
    assert card.status == "ok"
    assert card.reason is None
    by_name = {w.name: w for w in card.windows}
    assert by_name["Claude 5 Hour"].used_percent == 10.0
    assert by_name["Claude 5 Hour"].window_seconds == 5 * 3600
    assert by_name["Claude 7 Day"].used_percent == 100.0
    assert by_name["Claude 7 Day"].window_seconds == 7 * 86400
    assert by_name["Claude 7 Day"].resets_at is not None


def test_missing_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_LIMITS_OMP_DB", raising=False)
    assert collect_omp(tmp_path) == []


def test_stale_db_path_env_override_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_LIMITS_OMP_DB", str(tmp_path / "no-such-file.db"))
    assert collect_omp(tmp_path) == []


def test_default_db_path_used_without_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_LIMITS_OMP_DB", raising=False)
    now_ms = int(time.time() * 1000)
    db_path = tmp_path / ".omp" / "agent" / "agent.db"
    conn = _make_db(db_path)
    _insert_usage(
        conn,
        provider="zai",
        account_key="api_key|secret:z1",
        email=None,
        limit_id="zai:tokens:5h",
        label="ZAI 5 Hours Token Quota",
        window_label="5 Hours",
        used_fraction=0.25,
        status="ok",
        resets_at=None,
        recorded_at=now_ms,
    )
    conn.commit()
    conn.close()

    cards = collect_omp(tmp_path)
    assert len(cards) == 1
    assert cards[0].provider == "z.ai (omp)"
    assert cards[0].account == "secret:z1"
    assert cards[0].windows[0].used_percent == 25.0


def test_disabled_credential_marks_auth_failed(tmp_path, monkeypatch):
    now_ms = int(time.time() * 1000)
    db_path = tmp_path / "agent.db"
    conn = _make_db(db_path)
    _insert_usage(
        conn,
        provider="kimi-code",
        account_key="oauth|account:xyz",
        email=None,
        limit_id="kimi-code:1",
        label="5h limit",
        window_label="5h limit",
        used_fraction=0.5,
        status="ok",
        resets_at=None,
        recorded_at=now_ms,
    )
    _insert_credential(
        conn,
        provider="kimi-code",
        identity_key="account:xyz",
        disabled_cause="oauth refresh failed: invalid_grant",
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HARNESS_LIMITS_OMP_DB", str(db_path))
    cards = collect_omp(tmp_path)

    assert len(cards) == 1
    card = cards[0]
    assert card.status == "auth_failed"
    assert card.reason == "oauth refresh failed: invalid_grant"
    # Usage data is still surfaced — auth failure and usage exhaustion are distinct facts.
    assert card.windows[0].used_percent == 50.0


def test_stale_ghost_account_key_filtered_out(tmp_path, monkeypatch):
    """kimi-code's account_key churns across broken-refresh sessions in omp's own
    bookkeeping; only the provider's freshest polling batch should survive."""
    now_ms = int(time.time() * 1000)
    thirty_days_ago_ms = now_ms - 30 * 86_400_000
    db_path = tmp_path / "agent.db"
    conn = _make_db(db_path)
    _insert_usage(
        conn,
        provider="kimi-code",
        account_key="oauth|account:fresh",
        email=None,
        limit_id="kimi-code:1",
        label="5h limit",
        window_label="5h limit",
        used_fraction=0.2,
        status="ok",
        resets_at=None,
        recorded_at=now_ms,
    )
    _insert_usage(
        conn,
        provider="kimi-code",
        account_key="oauth|secret:ghost1",
        email=None,
        limit_id="kimi-code:1",
        label="5h limit",
        window_label="5h limit",
        used_fraction=0.9,
        status="ok",
        resets_at=None,
        recorded_at=thirty_days_ago_ms,
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HARNESS_LIMITS_OMP_DB", str(db_path))
    cards = collect_omp(tmp_path)

    assert len(cards) == 1
    assert cards[0].account == "account:fresh"


def test_collect_all_includes_omp_cards_without_crashing_when_db_absent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cards = collect_all(tmp_path)
    assert isinstance(cards, list)
    assert len(cards) >= 4
    assert not any("(omp)" in c.provider for c in cards)


def test_collect_all_appends_omp_cards_after_native_ones(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    db_path = tmp_path / ".omp" / "agent" / "agent.db"
    conn = _make_db(db_path)
    _insert_usage(
        conn,
        provider="xai-oauth",
        account_key="oauth|account:g1|email:g@x.com",
        email="g@x.com",
        limit_id="xai-oauth:credits:1w",
        label="SuperGrok Weekly Credits",
        window_label="Weekly",
        used_fraction=1.0,
        status="exhausted",
        resets_at=now_ms + 86_400_000,
        recorded_at=now_ms,
    )
    conn.commit()
    conn.close()

    cards = collect_all(tmp_path)
    omp_cards = [c for c in cards if "(omp)" in c.provider]
    assert len(omp_cards) == 1
    assert omp_cards[0].provider == "x.ai (omp)"
    assert omp_cards[0].windows[0].used_percent == 100.0
    # collect_all APPENDS omp cards after every native collector's cards.
    assert cards[-1] is omp_cards[0]
