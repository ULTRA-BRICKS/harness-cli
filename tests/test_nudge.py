from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from harness_cli import cli, nudge, omp_acp


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("HARNESS_QUESTIONS_DIR", str(tmp_path / "questions"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def _write_jsonl(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _seed_live_omp_session(
    tmp_path: Path, *, cwd: str = "/tmp/proj", session_id: str = "sess-abc"
) -> Path:
    path = tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "sess.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
            {"type": "session", "id": session_id, "cwd": cwd, "title": "t"},
        ],
    )
    return path


_FAKE_OMP_RESPOND = """#!/usr/bin/env python3
import json
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    call_id = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": call_id, "result": {"agentInfo": {"name": "oh-my-pi"}}})
    elif method == "session/load":
        params = msg.get("params") or {}
        send({"jsonrpc": "2.0", "id": call_id, "result": {"sessionId": params.get("sessionId")}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "id": call_id, "result": {}})
        break
"""

_FAKE_OMP_HANG_ON_PROMPT = """#!/usr/bin/env python3
import json
import sys
import time


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    call_id = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": call_id, "result": {}})
    elif method == "session/load":
        params = msg.get("params") or {}
        send({"jsonrpc": "2.0", "id": call_id, "result": {"sessionId": params.get("sessionId")}})
    elif method == "session/prompt":
        time.sleep(3600)
"""


def _install_fake_omp(tmp_path: Path, monkeypatch, script: str) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    omp_path = bin_dir / "omp"
    omp_path.write_text(script, encoding="utf-8")
    omp_path.chmod(omp_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def test_resolve_target_reads_cwd_and_session_id_from_session_line(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    path = _seed_live_omp_session(tmp_path, cwd="/Users/x/proj", session_id="sess-123")

    target = omp_acp.resolve_target(path)

    assert target == {"cwd": "/Users/x/proj", "session_id": "sess-123"}


def test_resolve_target_returns_none_without_a_session_record(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    path = tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "no-session.jsonl"
    _write_jsonl(path, [{"type": "message", "message": {"role": "assistant", "content": "hi"}}])

    assert omp_acp.resolve_target(path) is None


def test_resolve_target_returns_none_for_missing_file(tmp_path):
    assert omp_acp.resolve_target(tmp_path / "missing.jsonl") is None


def test_nudge_unparseable_agent_id_is_not_found():
    result = nudge.send_nudge("not-an-agent-id", "hi", 1.0)
    assert result.ok is False
    assert result.reason == "not-found"
    assert result.exit_code() == 3


def test_nudge_unknown_harness_is_not_found(tmp_path):
    path = tmp_path / "whatever.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    result = nudge.send_nudge(f"codex:parent:{path}", "hi", 1.0)
    assert result.ok is False
    assert result.reason == "not-found"


def test_nudge_missing_path_is_not_found(tmp_path):
    result = nudge.send_nudge(f"omp:parent:{tmp_path / 'gone.jsonl'}", "hi", 1.0)
    assert result.ok is False
    assert result.reason == "not-found"


def test_nudge_claude_kind_is_unsupported_via_cli(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    path = tmp_path / ".claude" / "projects" / "-proj" / "parent-sess.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")

    rc = cli.main(["nudge", f"claude:parent:{path}", "hello", "--json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert out["ok"] is False
    assert out["reason"] == "unsupported"
    assert "Claude Code" in out["error"]


def test_nudge_omp_sends_prompt_through_fake_acp_subprocess(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    path = _seed_live_omp_session(tmp_path, cwd=str(tmp_path), session_id="sess-live")
    _install_fake_omp(tmp_path, monkeypatch, _FAKE_OMP_RESPOND)

    rc = cli.main(["nudge", f"omp:parent:{path}", "you have been silent", "--json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out == {"ok": True}


def test_nudge_omp_missing_binary_is_failed(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    path = _seed_live_omp_session(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    rc = cli.main(["nudge", f"omp:parent:{path}", "hi", "--json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["ok"] is False
    assert out["reason"] == "failed"
    assert "PATH" in out["error"]


def test_nudge_omp_prompt_timeout_is_failed(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    path = _seed_live_omp_session(tmp_path, cwd=str(tmp_path), session_id="sess-hang")
    _install_fake_omp(tmp_path, monkeypatch, _FAKE_OMP_HANG_ON_PROMPT)

    rc = cli.main(["nudge", f"omp:parent:{path}", "hi", "--json", "--timeout-ms", "300"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["ok"] is False
    assert out["reason"] == "failed"
    assert "timed out" in out["error"].lower()


def test_nudge_text_mode_prints_sent_on_success(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    path = _seed_live_omp_session(tmp_path, cwd=str(tmp_path), session_id="sess-text")
    _install_fake_omp(tmp_path, monkeypatch, _FAKE_OMP_RESPOND)

    rc = cli.main(["nudge", f"omp:parent:{path}", "hi"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out.strip() == "sent"


def test_nudge_help_lists_timeout_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["nudge", "--help"])
    help_text = capsys.readouterr().out
    assert "--timeout-ms" in help_text
