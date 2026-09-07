from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness_cli import cli, discovery

_REPO = Path(__file__).resolve().parent.parent


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write_transcript(path, tokens_out=100, when: datetime | None = None):
    # `when` defaults to "now" (not a hardcoded date) so window-hours-filtered tests stay
    # correct regardless of when the suite actually runs.
    when = when or datetime.now(timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": _iso(when), "message": {"role": "user"}},
        {
            "type": "assistant",
            "timestamp": _iso(when + timedelta(seconds=5)),
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": tokens_out,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    ]
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)


def test_no_command_prints_help(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "harness" in out.lower()


def test_limits_current_reports_no_transcript_when_none_exists(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    cwd_dir = tmp_path / "somewhere"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    rc = cli.main(["limits", "--current"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "No transcript found" in out


def test_limits_current_reads_real_transcript_via_session_id(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    cwd_dir = tmp_path / "somewhere"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    slug = discovery.slugify_cwd(str(cwd_dir))
    project_dir = tmp_path / ".claude" / "projects" / slug
    transcript = project_dir / "sess-1.jsonl"
    _write_transcript(transcript, tokens_out=42)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")

    rc = cli.main(["limits", "--current"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "output tokens:             42" in out
    assert "$CLAUDE_CODE_SESSION_ID" in out


def test_limits_current_json_output_is_valid_json(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    cwd_dir = tmp_path / "somewhere"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    slug = discovery.slugify_cwd(str(cwd_dir))
    project_dir = tmp_path / ".claude" / "projects" / slug
    transcript = project_dir / "sess-1.jsonl"
    _write_transcript(transcript, tokens_out=7)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")

    rc = cli.main(["limits", "--current", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    data = json.loads(out)
    assert "$CLAUDE_CODE_SESSION_ID" in data["resolution"]
    assert data["session"]["usage"]["output_tokens"] == 7


def test_limits_all_aggregates_across_accounts(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)

    _write_transcript(
        tmp_path / ".claude" / "projects" / "proj-a" / "s1.jsonl", tokens_out=10
    )
    _write_transcript(
        tmp_path / ".claude-accounts" / "account-0" / "projects" / "proj-b" / "s2.jsonl",
        tokens_out=20,
    )

    rc = cli.main(["limits", "--all", "--window-hours", "0"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "[default]" in out
    assert "[account-0]" in out
    assert "TOTAL tokens observed:" in out


def test_limits_all_excludes_stale_content_despite_fresh_mtime(tmp_path, monkeypatch, capsys):
    # Regression: a file's mtime can be recent (e.g. touched by an external process) while
    # its actual last MESSAGE is old. The window filter must trust parsed content, not just
    # mtime, or "--window-hours 5" silently includes months-old sessions in the total.
    import os as _os

    _isolate_home(tmp_path, monkeypatch)
    stale_time = datetime.now(timezone.utc) - timedelta(days=400)
    stale = tmp_path / ".claude" / "projects" / "proj-a" / "old.jsonl"
    _write_transcript(stale, tokens_out=999, when=stale_time)
    now = time.time()
    _os.utime(stale, (now, now))  # fresh mtime, stale content

    fresh = tmp_path / ".claude" / "projects" / "proj-b" / "new.jsonl"
    _write_transcript(fresh, tokens_out=1)  # defaults to "now"

    rc = cli.main(["limits", "--all", "--window-hours", "5", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)

    assert rc == 0
    session_ids = [s["session_id"] for s in data]
    assert "old" not in session_ids
    assert "new" in session_ids


def test_limits_all_ignores_fresh_non_message_timestamp_on_stale_session(
    tmp_path, monkeypatch, capsys
):
    # Regression: a same-file "attachment"-type record with a FRESH timestamp must not
    # make an otherwise months-old session look recent - only user/assistant records
    # count as real content activity (see transcripts.py::_apply_entry).
    _isolate_home(tmp_path, monkeypatch)
    stale_time = datetime.now(timezone.utc) - timedelta(days=400)
    path = tmp_path / ".claude" / "projects" / "proj-a" / "old.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {
            "type": "assistant",
            "timestamp": _iso(stale_time),
            "message": {
                "model": "claude-sonnet-5",
                "usage": {"output_tokens": 999},
            },
        },
        {"type": "attachment", "timestamp": _iso(datetime.now(timezone.utc))},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")

    rc = cli.main(["limits", "--all", "--window-hours", "5", "--json"])
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert data == []


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


def test_limits_all_skips_non_dict_json_lines(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    path = tmp_path / ".claude" / "projects" / "proj-a" / "weird.jsonl"
    path.parent.mkdir(parents=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("[]\n")
        fh.write('"just a string"\n')
        fh.write("null\n")
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": _iso(datetime.now(timezone.utc)),
                    "message": {"model": "claude-sonnet-5", "usage": {"output_tokens": 3}},
                }
            )
            + "\n"
        )

    rc = cli.main(["limits", "--all", "--window-hours", "5", "--json"])
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert len(data) == 1
    assert data[0]["usage"]["output_tokens"] == 3


def test_limits_all_json_is_valid_and_flat_list(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    _write_transcript(tmp_path / ".claude" / "projects" / "proj-a" / "s1.jsonl", tokens_out=5)

    rc = cli.main(["limits", "--all", "--window-hours", "0", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, list)
    assert data[0]["usage"]["output_tokens"] == 5


def test_limits_current_tolerates_transcript_vanishing_before_read(
    tmp_path, monkeypatch, capsys
):
    # Regression: find_current_transcript() can resolve a path that's deleted/rotated
    # before parse_transcript() opens it. --current must report "no session", not crash,
    # matching the OSError tolerance --all already had.
    _isolate_home(tmp_path, monkeypatch)
    cwd_dir = tmp_path / "somewhere"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    slug = discovery.slugify_cwd(str(cwd_dir))
    project_dir = tmp_path / ".claude" / "projects" / slug
    transcript = project_dir / "sess-1.jsonl"
    _write_transcript(transcript)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")

    from harness_cli import transcripts as transcripts_module

    def vanished(*args, **kwargs):
        raise FileNotFoundError(transcript)

    # cli._run_limits does `from harness_cli.transcripts import parse_transcript` fresh
    # on every call, so patching the module-level function is what actually takes effect.
    monkeypatch.setattr(transcripts_module, "parse_transcript", vanished)

    rc = cli.main(["limits", "--current"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "vanished" in out.lower()


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
