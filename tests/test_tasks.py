from __future__ import annotations

import json
import os
import stat
import subprocess
import urllib.request

import pytest

from harness_cli import cli

_TITLE_263 = "Review documentation diff for performance regressions"


def test_top_level_help_lists_tasks(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tasks" in out


def test_tasks_help_lists_new_list_done(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["tasks", "--help"])
    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    help_text = captured.out
    assert "new" in help_text
    assert "list" in help_text
    assert "done" in help_text


def test_process_ticket_does_not_create_github_product_issue(tmp_path, monkeypatch, capsys):
    # Regression for conloca/landing#263: a process ticket must stay in the local
    # store even when cwd is a product repo whose origin is GitHub. A fake `gh` and
    # a patched urlopen prove the CLI never files a GitHub issue as a side effect.
    store = tmp_path / "tasks"
    repo = tmp_path / "landing"
    home = tmp_path / "home"
    sentinel = tmp_path / "gh-was-invoked"
    bin_dir = tmp_path / "bin"
    store.mkdir()
    repo.mkdir()
    home.mkdir()
    bin_dir.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.setenv("TASK_SESSION", "fix-how-it-works-fidelity")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/conloca/landing.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f"printf invoked > {sentinel.as_posix()!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", os.pathsep.join((str(bin_dir), os.environ.get("PATH", ""))))
    monkeypatch.chdir(repo)

    def _no_network(*_args, **_kwargs):
        raise AssertionError("urllib.request.urlopen must not be called")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    rc = cli.main(["tasks", "new", "--title", _TITLE_263])
    captured = capsys.readouterr()
    assert rc == 0
    assert not sentinel.exists()
    json_files = list(store.glob("*.json"))
    assert len(json_files) == 1
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert payload["title"] == _TITLE_263
    assert payload["state"] == "open"
    raw = json_files[0].read_text(encoding="utf-8")
    assert "github.com/conloca/landing" not in raw
    assert _TITLE_263 in captured.out


def test_tasks_list_and_done_stay_local(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    rc = cli.main(["tasks", "new", "--title", "session chore"])
    assert rc == 0
    capsys.readouterr()

    rc = cli.main(["tasks", "list"])
    listed = capsys.readouterr().out
    assert rc == 0
    assert "session chore" in listed

    rc = cli.main(["tasks", "done", "1"])
    assert rc == 0
    capsys.readouterr()

    rc = cli.main(["tasks", "list"])
    hidden = capsys.readouterr().out
    assert rc == 0
    assert "session chore" not in hidden

    rc = cli.main(["tasks", "list", "--all"])
    shown = capsys.readouterr().out
    assert rc == 0
    assert "session chore" in shown
    assert "done" in shown


def test_tasks_done_unknown_id_exits_1(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    rc = cli.main(["tasks", "done", "99"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "99" in captured.out + captured.err


def test_tasks_new_strips_terminal_escapes(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    title = "\x1b]52;c;c3RvbGVu\x07looks-fine"
    rc = cli.main(["tasks", "new", "--title", title])
    captured = capsys.readouterr()
    assert rc == 0
    assert "\x1b" not in captured.out
    assert "looks-fine" in captured.out


def test_tasks_list_skips_malformed_id(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    store.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    (store / "1.json").write_text(
        json.dumps(
            {
                "id": "9" * 4301,
                "title": "huge",
                "body": "",
                "state": "open",
                "created_at": "2026-01-01T00:00:00Z",
                "session": "",
                "labels": ["agent"],
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(["tasks", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "huge" not in captured.out


