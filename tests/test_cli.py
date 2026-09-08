from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness_cli import cli

_REPO = Path(__file__).resolve().parent.parent
_RESET = datetime(2026, 9, 8, 15, 12, 0, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_WEEKLY_RESET = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_LIMITS_KEYCHAIN", "0")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


def _write_claude_live(tmp_path: Path, email: str = "invntrm@gmail.com") -> None:
    root = tmp_path / ".claude"
    root.mkdir(parents=True)
    (root / "projects").mkdir()
    (root / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": email},
                "organizationRateLimitTier": "default_claude_max_20x",
            }
        ),
        encoding="utf-8",
    )
    (root / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-not-for-log",
                    "refreshToken": "refresh-not-for-log",
                    "expiresAt": 4102444800000,
                }
            }
        ),
        encoding="utf-8",
    )


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self, n=-1):
        return self._payload if n is None or n < 0 else self._payload[:n]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _mock_claude_usage(monkeypatch) -> None:
    payload = {
        "five_hour": {"utilization": 42.0, "resets_at": _RESET},
        "seven_day": {"utilization": 81.0, "resets_at": _WEEKLY_RESET},
        "seven_day_fable": {"utilization": 96.0, "resets_at": _WEEKLY_RESET},
        "seven_day_opus": None,
    }

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", None) or str(req)
        if "api.anthropic.com" in url and "oauth/usage" in url:
            return _Resp(payload)
        raise RuntimeError(f"unexpected url in unit test: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_no_command_prints_help(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "harness" in out.lower()


def test_limits_all_text_is_proximity_not_token_table(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    _write_claude_live(tmp_path)
    _mock_claude_usage(monkeypatch)

    rc = cli.main(["limits", "--all"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "TOTAL tokens observed" not in out
    assert "total tokens:" not in out.lower()
    assert "% used" in out
    assert "█" in out
    assert "Claude Code" in out
    assert "invntrm@gmail.com" in out


def test_limits_all_json_is_cards_and_windows(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    _write_claude_live(tmp_path)
    _mock_claude_usage(monkeypatch)

    rc = cli.main(["limits", "--all", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, dict)
    assert "cards" in data
    claude = next(c for c in data["cards"] if c["provider"] == "Claude Code")
    assert "windows" in claude
    names = [w["name"] for w in claude["windows"]]
    assert "Session" in names
    assert "Weekly" in names
    assert "Weekly·Fable" in names
    assert all("usage" not in w for w in claude["windows"])


def test_limits_current_uses_same_proximity_renderer(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    _write_claude_live(tmp_path)
    _mock_claude_usage(monkeypatch)

    rc = cli.main(["limits", "--current"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "% used" in out
    assert "█" in out
    assert "output tokens:" not in out.lower()
    assert "TOTAL tokens observed" not in out


def test_limits_current_json_is_cards(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    _write_claude_live(tmp_path)
    _mock_claude_usage(monkeypatch)

    rc = cli.main(["limits", "--current", "--json"])
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert "cards" in data
    assert "session" not in data or data.get("session") is None
    assert data["cards"][0]["windows"]


def test_limits_all_does_not_fake_percent_from_transcripts(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "projects").mkdir()
    (claude / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "invntrm@gmail.com"}}),
        encoding="utf-8",
    )
    transcript = claude / "projects" / "proj" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-08T11:00:00.000Z",
                "message": {"usage": {"output_tokens": 99999}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rc = cli.main(["limits", "--all"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "TOTAL tokens observed" not in out
    assert "total tokens:" not in out.lower()
    assert "% used" not in out
    assert "missing credentials" in out.lower() or "not guessed" in out.lower()


def test_limits_all_rejects_huge_finite_window_hours(capsys):
    # 1e308 is finite (passes math.isfinite) but overflows timedelta(hours=...) with
    # "OverflowError: Python int too large to convert to C int" - must be rejected by
    # argument validation before it ever reaches timedelta().
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["limits", "--all", "--window-hours", "1e308"])
    assert exc_info.value.code == 2
    assert "window-hours" in capsys.readouterr().err.lower()


def test_limits_all_rejects_infinite_window_hours(capsys):
    # argparse type errors call parser.error() -> sys.exit(2), not a normal return.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["limits", "--all", "--window-hours", "inf"])
    assert exc_info.value.code == 2
    assert "window-hours" in capsys.readouterr().err.lower()


def test_limits_all_rejects_negative_window_hours(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["limits", "--all", "--window-hours", "-1"])
    assert exc_info.value.code == 2
    assert "window-hours" in capsys.readouterr().err.lower()


def test_package_import_is_stdlib_only():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_REPO)!r})\n"
        "import harness_cli.cli\n"
        "harness_cli.cli.build_parser()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
