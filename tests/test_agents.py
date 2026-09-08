from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness_cli import cli

_REPO = Path(__file__).resolve().parent.parent


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


def _text_entry(kind: str, text: str) -> dict:
    if kind == "omp":
        return {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _seed_sessions(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "omp_parent": tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "sess.jsonl",
        "omp_nested": (
            tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "sess" / "NestedAgent.jsonl"
        ),
        "cc_parent": tmp_path / ".claude" / "projects" / "-proj" / "parent-sess.jsonl",
        "cc_nested": (
            tmp_path
            / ".claude"
            / "projects"
            / "-proj"
            / "parent-sess"
            / "subagents"
            / "agent-abc.jsonl"
        ),
    }
    _write_jsonl(paths["omp_parent"], [_text_entry("omp", "omp parent snippet")])
    _write_jsonl(paths["omp_nested"], [_text_entry("omp", "omp nested snippet")])
    _write_jsonl(paths["cc_parent"], [_text_entry("cc", "cc parent snippet")])
    _write_jsonl(paths["cc_nested"], [_text_entry("cc", "cc nested snippet")])
    return paths


def test_top_level_help_lists_agents(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "agents" in out


def test_python_module_help_lists_agents():
    result = subprocess.run(
        [sys.executable, "-m", "harness_cli", "--help"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )
    assert result.returncode == 0, result.stderr
    assert "agents" in result.stdout


def test_agents_help_lists_web(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["agents", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "web" in help_text


def test_agents_lists_omp_and_cc_parent_and_nested(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    _seed_sessions(tmp_path)

    rc = cli.main(["agents"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "omp parent snippet" in out
    assert "omp nested snippet" in out
    assert "cc parent snippet" in out
    assert "cc nested snippet" in out
    assert "NestedAgent" in out
    lowered = out.lower()
    assert "10 minute" in lowered or "10 min" in lowered


def test_agents_json_includes_tasks_and_questions(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    _seed_sessions(tmp_path)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "1.json").write_text(
        json.dumps({"id": 1, "title": "open landscape ticket", "state": "open"}),
        encoding="utf-8",
    )
    (tasks / "2.json").write_text(
        json.dumps({"id": 2, "title": "done landscape ticket", "state": "done"}),
        encoding="utf-8",
    )
    questions = tmp_path / "questions"
    questions.mkdir()
    (questions / "tg-ctl.123.questions.json").write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "req": {
                            "question": "Ship the landscape?",
                            "title": "Deploy",
                            "kind": "permission",
                            "agent": "Main",
                            "sessionName": "feat-agents-web",
                            "paneId": "%1",
                            "cwd": "/tmp",
                            "subagent": "",
                            "toolInput": {"secret": "must-not-appear"},
                        },
                        "at": "2026-09-08T00:00:00Z",
                        "messageId": 42,
                    }
                ],
                "answered": [
                    {
                        "req": {"question": "already answered"},
                        "at": "2026-09-07T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = cli.main(["agents", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "agents" in data
    assert "questions" in data
    assert "tasks" in data
    harnesses = {(row["harness"], row["kind"]) for row in data["agents"]}
    assert ("omp", "parent") in harnesses
    assert ("omp", "subagent") in harnesses
    assert ("claude", "parent") in harnesses
    assert ("claude", "subagent") in harnesses
    titles = [row.get("title") for row in data["tasks"]]
    assert "open landscape ticket" in titles
    assert "done landscape ticket" not in titles
    questions_text = json.dumps(data["questions"])
    assert "Ship the landscape?" in questions_text
    assert "must-not-appear" not in questions_text
    assert "already answered" not in questions_text
    dumped = json.dumps(data)
    assert "must-not-appear" not in dumped


def test_agents_all_includes_old_session(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    old = tmp_path / ".omp" / "agent" / "sessions" / "-old" / "ancient.jsonl"
    _write_jsonl(old, [_text_entry("omp", "ancient snippet")])
    old_mtime = datetime.now(timezone.utc).timestamp() - 48 * 3600
    os.utime(old, (old_mtime, old_mtime))

    rc = cli.main(["agents", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    texts = [row.get("last_text", "") for row in data["agents"]]
    assert "ancient snippet" not in texts

    rc = cli.main(["agents", "--all", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    texts = [row.get("last_text", "") for row in data["agents"]]
    assert "ancient snippet" in texts


def test_agents_skips_malformed_and_corrupt_questions(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    bad = tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "broken.jsonl"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not-json\n{also broken}\n", encoding="utf-8")
    questions = tmp_path / "questions"
    questions.mkdir()
    (questions / "tg-ctl.9.questions.json").write_text("not-json", encoding="utf-8")

    rc = cli.main(["agents", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["questions"] == []
    assert isinstance(data["agents"], list)


def test_agents_states_window_and_heuristic(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = cli.main(["agents"])
    out = capsys.readouterr().out.lower()
    assert rc == 0
    assert "24" in out
    assert "parked" in out
    assert "live" in out
