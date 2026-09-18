from __future__ import annotations

import json
import os
import stat
import subprocess
import urllib.request

import pytest

from harness_cli import cli, tasks

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


def test_old_singular_task_command_is_gone(capsys):
    # Regression for the task -> tasks rename: the old singular form must not
    # linger as a silent alias. It should fail exactly like any other unknown
    # top-level subcommand (argparse "invalid choice"), not be resolved.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["task", "list"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "'task'" in err


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
        f"#!/bin/sh\nprintf invoked > {sentinel.as_posix()!r}\nexit 0\n",
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


def test_create_ticket_retries_past_id_collision(tmp_path, monkeypatch):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    real_next_id = tasks._next_id
    calls = {"n": 0}

    def stuck_next_id(store_path):
        calls["n"] += 1
        return 1 if calls["n"] <= 2 else real_next_id(store_path)

    monkeypatch.setattr(tasks, "_next_id", stuck_next_id)

    first = tasks.create_ticket("first")
    second = tasks.create_ticket("second")

    assert first["id"] != second["id"]
    assert json.loads((store / f"{first['id']}.json").read_text())["title"] == "first"
    assert json.loads((store / f"{second['id']}.json").read_text())["title"] == "second"


def test_create_ticket_exhausted_retries_raises_instead_of_clobbering(tmp_path, monkeypatch):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    original = tasks.create_ticket("original")
    original_path = store / f"{original['id']}.json"
    original_bytes = original_path.read_bytes()

    monkeypatch.setattr(tasks, "_next_id", lambda store_path: original["id"])

    with pytest.raises(RuntimeError):
        tasks.create_ticket("colliding")

    assert original_path.read_bytes() == original_bytes


def _setup_store(tmp_path, monkeypatch):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return store


def _write_import_file(tmp_path, records):
    path = tmp_path / "import.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_import_creates_tickets(tmp_path, monkeypatch, capsys):
    store = _setup_store(tmp_path, monkeypatch)
    records = [
        {
            "source": "grooming-v4",
            "external_id": "v4-001",
            "title": "First groomed item",
            "body": "Evidence for the first item.",
            "state": "blocked",
            "group": "rig-core",
            "phase": None,
            "blocked_reason": None,
        },
        {
            "source": "grooming-v4",
            "external_id": "v4-002",
            "title": "Second groomed item",
            "body": "Evidence for the second item.",
            "state": "done",
        },
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 created / 0 updated / 2 total" in out

    all_tickets = tasks.list_tickets(include_done=True)
    assert len(all_tickets) == 2
    by_external = {t["external_id"]: t for t in all_tickets}
    assert by_external["v4-001"]["title"] == "First groomed item"
    assert by_external["v4-001"]["state"] == "blocked"
    assert by_external["v4-001"]["group"] == "rig-core"
    assert by_external["v4-001"]["source"] == "grooming-v4"
    assert by_external["v4-001"]["duplicate_of"] is None
    assert by_external["v4-002"]["state"] == "done"
    assert len(list(store.glob("*.json"))) == 2


def test_import_reimport_is_noop(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    records = [
        {
            "source": "grooming-v4",
            "external_id": "v4-010",
            "title": "Idempotent item",
            "body": "Body text.",
            "state": "open",
        }
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()
    assert rc == 0

    rc = cli.main(["tasks", "import", str(import_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 created / 1 updated / 1 total" in out

    all_tickets = tasks.list_tickets(include_done=True)
    assert len(all_tickets) == 1


def test_import_updates_changed_title_and_state_in_place(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    first_pass = [
        {
            "source": "grooming-v4",
            "external_id": "v4-020",
            "title": "Original title",
            "body": "Original body.",
            "state": "open",
        }
    ]
    import_path = _write_import_file(tmp_path, first_pass)
    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()

    before = tasks.list_tickets(include_done=True)
    assert len(before) == 1
    original_id = before[0]["id"]
    original_created_at = before[0]["created_at"]

    # v4-020 now duplicates v4-999, a record defined *after* it in the same
    # batch -- exercises forward-reference resolution -- and duplicate_of
    # must land on v4-999's real internal ticket id, not the literal "999".
    second_pass = [
        {
            "source": "grooming-v4",
            "external_id": "v4-020",
            "title": "Updated title",
            "body": "Original body.",
            "state": "duplicate",
            "duplicate_of_external_id": "v4-999",
        },
        {
            "source": "grooming-v4",
            "external_id": "v4-999",
            "title": "The canonical item",
            "body": "Target of the duplicate.",
            "state": "done",
        },
    ]
    import_path = _write_import_file(tmp_path, second_pass)
    rc = cli.main(["tasks", "import", str(import_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 created / 1 updated / 2 total" in out

    after = tasks.list_tickets(include_done=True)
    assert len(after) == 2
    by_external = {t["external_id"]: t for t in after}
    updated = by_external["v4-020"]
    target = by_external["v4-999"]
    assert updated["id"] == original_id
    assert updated["created_at"] == original_created_at
    assert updated["title"] == "Updated title"
    assert updated["state"] == "duplicate"
    assert updated["duplicate_of"] == target["id"]
    assert updated["duplicate_of"] != 999


def test_default_list_excludes_non_open_states(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    records = [
        {"source": "grooming-v4", "external_id": "v4-030", "title": "still open", "state": "open"},
        {
            "source": "grooming-v4",
            "external_id": "v4-031",
            "title": "already done",
            "state": "done",
        },
        {
            "source": "grooming-v4",
            "external_id": "v4-032",
            "title": "genuinely blocked",
            "state": "blocked",
        },
        {
            "source": "grooming-v4",
            "external_id": "v4-033",
            "title": "dup of something",
            "state": "duplicate",
        },
        {
            "source": "grooming-v4",
            "external_id": "v4-034",
            "title": "unrecoverable",
            "state": "dropped",
        },
    ]
    import_path = _write_import_file(tmp_path, records)
    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()

    rc = cli.main(["tasks", "list"])
    default_out = capsys.readouterr().out
    assert rc == 0
    assert "still open" in default_out
    assert "already done" not in default_out
    assert "genuinely blocked" not in default_out
    assert "dup of something" not in default_out
    assert "unrecoverable" not in default_out

    rc = cli.main(["tasks", "list", "--all"])
    all_out = capsys.readouterr().out
    assert rc == 0
    for title in (
        "still open",
        "already done",
        "genuinely blocked",
        "dup of something",
        "unrecoverable",
    ):
        assert title in all_out


def test_import_invalid_state_rejected_with_clear_error(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    records = [
        {
            "source": "grooming-v4",
            "external_id": "v4-099",
            "title": "bad state item",
            "state": "not-a-real-state",
        }
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not-a-real-state" in captured.err
    assert "state" in captured.err

    assert tasks.list_tickets(include_done=True) == []


def test_import_duplicate_external_id_within_file_updates_second_occurrence(
    tmp_path, monkeypatch, capsys
):
    _setup_store(tmp_path, monkeypatch)
    records = [
        {
            "source": "grooming-v4",
            "external_id": "v4-050",
            "title": "first pass title",
            "state": "open",
        },
        {
            "source": "grooming-v4",
            "external_id": "v4-050",
            "title": "second pass title",
            "state": "done",
        },
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 created / 1 updated / 2 total" in out

    all_tickets = tasks.list_tickets(include_done=True)
    assert len(all_tickets) == 1
    assert all_tickets[0]["title"] == "second pass title"
    assert all_tickets[0]["state"] == "done"


def test_import_unresolvable_duplicate_target_rejected_atomically(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    records = [
        {"source": "grooming-v4", "external_id": "v4-060", "title": "ok item", "state": "open"},
        {
            "source": "grooming-v4",
            "external_id": "v4-061",
            "title": "dangling duplicate",
            "state": "duplicate",
            "duplicate_of_external_id": "v4-999-does-not-exist",
        },
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "v4-999-does-not-exist" in captured.err

    # Atomic: the unresolvable duplicate target must block the *entire*
    # batch, including the otherwise-valid preceding record.
    assert tasks.list_tickets(include_done=True) == []


def test_import_missing_file_returns_error(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    rc = cli.main(["tasks", "import", str(tmp_path / "does-not-exist.json")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "does-not-exist.json" in captured.err


def test_import_invalid_json_returns_error(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    rc = cli.main(["tasks", "import", str(bad_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "invalid JSON" in captured.err


def test_import_non_array_json_returns_error(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    obj_path = tmp_path / "object.json"
    obj_path.write_text(json.dumps({"source": "grooming-v4"}), encoding="utf-8")
    rc = cli.main(["tasks", "import", str(obj_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "JSON array" in captured.err


def test_import_blocked_by_stale_lock_raises_clear_error(tmp_path, monkeypatch, capsys):
    store = _setup_store(tmp_path, monkeypatch)
    store.mkdir(parents=True, exist_ok=True)
    (store / tasks._IMPORT_LOCK_NAME).touch()
    monkeypatch.setattr(tasks, "_IMPORT_LOCK_TIMEOUT", 0.05)
    monkeypatch.setattr(tasks, "_IMPORT_LOCK_POLL", 0.01)

    records = [
        {
            "source": "grooming-v4",
            "external_id": "v4-070",
            "title": "blocked by lock",
            "state": "open",
        }
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "in progress" in captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_import_releases_lock_after_success(tmp_path, monkeypatch, capsys):
    store = _setup_store(tmp_path, monkeypatch)
    records = [
        {"source": "grooming-v4", "external_id": "v4-071", "title": "clean run", "state": "open"}
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()
    assert rc == 0
    assert not (store / tasks._IMPORT_LOCK_NAME).exists()


def test_import_self_referential_duplicate_rejected(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    records = [
        {
            "source": "grooming-v4",
            "external_id": "v4-080",
            "title": "self duplicate",
            "state": "duplicate",
            "duplicate_of_external_id": "v4-080",
        }
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "cannot reference itself" in captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_import_null_title_rejected(tmp_path, monkeypatch, capsys):
    _setup_store(tmp_path, monkeypatch)
    records = [{"source": "grooming-v4", "external_id": "v4-081", "title": None, "state": "open"}]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "title" in captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_import_reclaims_stale_lock(tmp_path, monkeypatch, capsys):
    import os as _os
    import time as _time

    store = _setup_store(tmp_path, monkeypatch)
    store.mkdir(parents=True, exist_ok=True)
    lock_path = store / tasks._IMPORT_LOCK_NAME
    lock_path.touch()
    stale_mtime = _time.time() - 3600  # 1 hour old: well past any staleness threshold
    _os.utime(lock_path, (stale_mtime, stale_mtime))
    monkeypatch.setattr(tasks, "_IMPORT_LOCK_STALE_AFTER", 1.0)

    records = [
        {
            "source": "grooming-v4",
            "external_id": "v4-082",
            "title": "reclaims stale lock",
            "state": "open",
        }
    ]
    import_path = _write_import_file(tmp_path, records)

    rc = cli.main(["tasks", "import", str(import_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 created / 0 updated / 1 total" in out
    assert not lock_path.exists()
