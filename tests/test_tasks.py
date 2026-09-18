from __future__ import annotations

import inspect
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
    assert "import" in help_text


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


def test_tasks_import_creates_tickets_with_extended_fields(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps(
            [
                {
                    "title": "Groomed item one",
                    "body": "Evidence for item one.",
                    "phase": None,
                    "group": "rig-core",
                    "state": "blocked",
                    "blocked_reason": "tracked as example-repo#1",
                    "duplicate_of": None,
                    "source": "grooming-v4",
                    "external_id": "v4-001",
                }
            ]
        ),
        encoding="utf-8",
    )

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "1 created, 0 updated, of 1 total" in captured.out

    tickets = tasks.list_tickets(include_done=True)
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket["title"] == "Groomed item one"
    assert ticket["group"] == "rig-core"
    assert ticket["state"] == "blocked"
    assert ticket["blocked_reason"] == "tracked as example-repo#1"
    assert ticket["source"] == "grooming-v4"
    assert ticket["external_id"] == "v4-001"


def test_tasks_import_is_idempotent_on_rerun(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    items = [
        {
            "title": f"Groomed item {i}",
            "body": "",
            "phase": None,
            "group": "rig-core",
            "state": "open",
            "blocked_reason": None,
            "duplicate_of": None,
            "source": "grooming-v4",
            "external_id": f"v4-{i:03d}",
        }
        for i in range(1, 4)
    ]
    import_path.write_text(json.dumps(items), encoding="utf-8")

    rc = cli.main(["tasks", "import", str(import_path)])
    first = capsys.readouterr().out
    assert rc == 0
    assert "3 created, 0 updated, of 3 total" in first
    first_ids = sorted(t["id"] for t in tasks.list_tickets(include_done=True))

    rc = cli.main(["tasks", "import", str(import_path)])
    second = capsys.readouterr().out
    assert rc == 0
    assert "0 created, 3 updated, of 3 total" in second
    second_ids = sorted(t["id"] for t in tasks.list_tickets(include_done=True))

    assert first_ids == second_ids
    assert len(tasks.list_tickets(include_done=True)) == 3


def test_tasks_import_updates_existing_ticket_on_changed_fields(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    item = {
        "title": "Original title",
        "body": "",
        "phase": None,
        "group": "rig-core",
        "state": "open",
        "blocked_reason": None,
        "duplicate_of": None,
        "source": "grooming-v4",
        "external_id": "v4-042",
    }
    import_path.write_text(json.dumps([item]), encoding="utf-8")
    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()
    original_id = tasks.list_tickets(include_done=True)[0]["id"]

    item["title"] = "Updated title"
    item["state"] = "done"
    import_path.write_text(json.dumps([item]), encoding="utf-8")
    rc = cli.main(["tasks", "import", str(import_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 created, 1 updated, of 1 total" in out

    tickets = tasks.list_tickets(include_done=True)
    assert len(tickets) == 1
    assert tickets[0]["id"] == original_id
    assert tickets[0]["title"] == "Updated title"
    assert tickets[0]["state"] == "done"


def test_tasks_list_default_excludes_blocked_and_duplicate_like_done(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    tasks.create_ticket("open item")
    tasks.create_ticket("done item", state="done")
    tasks.create_ticket("blocked item", state="blocked", blocked_reason="x")
    tasks.create_ticket("duplicate item", state="duplicate", duplicate_of=1)
    tasks.create_ticket("dropped item", state="dropped", blocked_reason="y")

    rc = cli.main(["tasks", "list"])
    default_view = capsys.readouterr().out
    assert rc == 0
    assert "open item" in default_view
    assert "done item" not in default_view
    assert "blocked item" not in default_view
    assert "duplicate item" not in default_view
    assert "dropped item" not in default_view

    rc = cli.main(["tasks", "list", "--all"])
    all_view = capsys.readouterr().out
    assert rc == 0
    for title in ("open item", "done item", "blocked item", "duplicate item", "dropped item"):
        assert title in all_view


def test_tasks_import_rejects_invalid_state(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps(
            [
                {
                    "title": "Bad item",
                    "body": "",
                    "state": "not-a-real-state",
                    "source": "grooming-v4",
                    "external_id": "v4-999",
                }
            ]
        ),
        encoding="utf-8",
    )

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not-a-real-state" in captured.out + captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_tasks_import_rejects_non_dict_item(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps([{"title": "ok", "external_id": "v4-1", "source": "s"}, None]), encoding="utf-8"
    )

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "must be a JSON object" in captured.out + captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_tasks_import_without_external_id_creates_new_ticket_every_run(
    tmp_path, monkeypatch, capsys
):
    # Records missing source/external_id have no dedup identity, so import_tickets
    # cannot upsert them - each run creates a fresh ticket rather than silently
    # matching an unrelated one on a coincidental key collision.
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(json.dumps([{"title": "keyless item", "body": ""}]), encoding="utf-8")

    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()
    cli.main(["tasks", "import", str(import_path)])
    out = capsys.readouterr().out

    assert "1 created, 0 updated, of 1 total" in out
    assert len(tasks.list_tickets(include_done=True)) == 2


def test_tasks_import_rejects_list_typed_state(tmp_path, monkeypatch, capsys):
    # A non-hashable state (e.g. a JSON array) must raise a clean ValueError before
    # any ticket is written, not a raw TypeError from frozenset membership testing.
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps([{"title": "bad state", "state": ["open"], "source": "s", "external_id": "e"}]),
        encoding="utf-8",
    )

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "must be a string" in captured.out + captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_tasks_import_rejects_list_typed_external_id_without_partial_write(
    tmp_path, monkeypatch, capsys
):
    # A non-hashable external_id would otherwise crash on the dict-key lookup mid-loop,
    # after an earlier valid record in the same file was already written to disk.
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps(
            [
                {"title": "good item", "source": "s", "external_id": "e1"},
                {"title": "bad item", "source": "s", "external_id": ["e2"]},
            ]
        ),
        encoding="utf-8",
    )

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "must be a string or null" in captured.out + captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_tasks_import_update_preserves_identity_fields(tmp_path, monkeypatch, capsys):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    item = {"title": "original", "source": "grooming-v4", "external_id": "v4-1"}
    import_path.write_text(json.dumps([item]), encoding="utf-8")
    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()
    before = tasks.list_tickets(include_done=True)[0]

    item["title"] = "changed"
    import_path.write_text(json.dumps([item]), encoding="utf-8")
    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()
    after = tasks.list_tickets(include_done=True)[0]

    assert after["id"] == before["id"]
    assert after["created_at"] == before["created_at"]
    assert after["session"] == before["session"]
    assert after["labels"] == before["labels"]
    assert after["source"] == before["source"]
    assert after["external_id"] == before["external_id"]
    assert after["title"] == "changed"


def test_tasks_import_raises_if_ticket_vanishes_between_scan_and_write(
    tmp_path, monkeypatch, capsys
):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    item = {"title": "original", "source": "grooming-v4", "external_id": "v4-2"}
    import_path.write_text(json.dumps([item]), encoding="utf-8")
    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()

    # Simulate the file vanishing between import_tickets' dedup scan (list_tickets)
    # and its write (_update_ticket) - a real race window this test cannot hit by
    # simply pre-deleting the file, since a fresh call would just re-scan and see it
    # already gone (falling through to create instead of update).
    monkeypatch.setattr(tasks, "_update_ticket", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="vanished during import"):
        tasks.import_tickets([item])


def test_grooming_fields_match_create_ticket_signature():
    # Pins the claim in the _GROOMING_FIELDS comment: every field it lists must be a
    # real create_ticket keyword parameter, or import_tickets' **kwargs splat would
    # raise TypeError at runtime for a field the comment claims is kept in sync.
    params = inspect.signature(tasks.create_ticket).parameters
    for field in tasks._GROOMING_FIELDS:
        assert field in params


def test_tasks_import_rejects_boolean_duplicate_of(tmp_path, monkeypatch, capsys):
    # bool is a subclass of int in Python; without an explicit isinstance(value, bool)
    # guard, JSON `"duplicate_of": true` would silently pass as duplicate_of=1.
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps([{"title": "x", "duplicate_of": True, "source": "s", "external_id": "e"}]),
        encoding="utf-8",
    )

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "'duplicate_of' must be an integer or null" in captured.out + captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_tasks_import_rejects_null_title(tmp_path, monkeypatch, capsys):
    # A present-but-null title is rejected rather than silently coerced to "" or
    # written as the string "None" - `title` is required content, not optional.
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps([{"title": None, "source": "s", "external_id": "e"}]), encoding="utf-8"
    )

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "'title' must be a string" in captured.out + captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_tasks_import_rejects_missing_title_key(tmp_path, monkeypatch, capsys):
    # A record with no `title` key at all must be rejected up front, not silently
    # default to "" - on the update path that would blank an existing ticket's title.
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(json.dumps([{"source": "s", "external_id": "e"}]), encoding="utf-8")

    rc = cli.main(["tasks", "import", str(import_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "'title' is required" in captured.out + captured.err
    assert tasks.list_tickets(include_done=True) == []


def test_tasks_import_missing_title_on_update_does_not_blank_existing_title(
    tmp_path, monkeypatch, capsys
):
    store = tmp_path / "tasks"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(store))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps([{"title": "keep me", "source": "s", "external_id": "e"}]), encoding="utf-8"
    )
    cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()

    import_path.write_text(json.dumps([{"source": "s", "external_id": "e"}]), encoding="utf-8")
    rc = cli.main(["tasks", "import", str(import_path)])
    capsys.readouterr()

    assert rc == 1
    ticket = tasks.list_tickets(include_done=True)[0]
    assert ticket["title"] == "keep me"
