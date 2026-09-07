from __future__ import annotations

import os
import time

from harness_cli import discovery


def test_slugify_cwd_matches_observed_convention():
    assert discovery.slugify_cwd("/Users/ultra/xp/rig-cli") == "-Users-ultra-xp-rig-cli"


def test_discover_account_dirs_finds_stock_and_rotated_layouts(tmp_path):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    (tmp_path / ".claude-accounts" / "account-0" / "projects").mkdir(parents=True)
    (tmp_path / ".claude-accounts" / "account-1" / "projects").mkdir(parents=True)
    # A dir without a projects/ subdir must not be treated as an account.
    (tmp_path / ".claude-accounts" / "not-an-account").mkdir(parents=True)

    dirs = discovery.discover_account_dirs(home=tmp_path)

    names = [d.name for d in dirs]
    assert names == [".claude", "account-0", "account-1"]


def test_discover_account_dirs_empty_when_nothing_present(tmp_path):
    assert discovery.discover_account_dirs(home=tmp_path) == []


def test_account_label_default_vs_named(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude-accounts" / "account-2").mkdir(parents=True)

    assert discovery.account_label(tmp_path / ".claude", home=tmp_path) == "default"
    assert (
        discovery.account_label(tmp_path / ".claude-accounts" / "account-2", home=tmp_path)
        == "account-2"
    )


def test_current_config_dir_honors_env_override(tmp_path, monkeypatch):
    override = tmp_path / "custom-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    assert discovery.current_config_dir(home=tmp_path) == override


def test_current_config_dir_defaults_to_dot_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert discovery.current_config_dir(home=tmp_path) == tmp_path / ".claude"


def test_find_current_transcript_resolves_via_session_id_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cwd = "/Users/ultra/xp/rig-cli"
    project_dir = tmp_path / ".claude" / "projects" / discovery.slugify_cwd(cwd)
    project_dir.mkdir(parents=True)
    target = project_dir / "session-abc.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-abc")
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)

    path, how = discovery.find_current_transcript(cwd=cwd, home=tmp_path)

    assert path == target
    assert "CLAUDE_CODE_SESSION_ID" in how


def test_find_current_transcript_falls_back_for_child_session(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cwd = "/Users/ultra/xp/rig-cli"
    project_dir = tmp_path / ".claude" / "projects" / discovery.slugify_cwd(cwd)
    subagent_dir = project_dir / "parent-session" / "subagents"
    subagent_dir.mkdir(parents=True)
    subagent_file = subagent_dir / "agent-xyz.jsonl"
    subagent_file.write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")

    path, how = discovery.find_current_transcript(cwd=cwd, home=tmp_path)

    assert path == subagent_file
    assert "subagent" in how


def test_find_current_transcript_rejects_path_traversal_session_id(tmp_path, monkeypatch):
    # A $CLAUDE_CODE_SESSION_ID containing ".." must not escape project_dir onto an
    # unrelated file that happens to exist elsewhere on disk.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cwd = "/Users/ultra/xp/rig-cli"
    project_dir = tmp_path / ".claude" / "projects" / discovery.slugify_cwd(cwd)
    project_dir.mkdir(parents=True)

    outside_target = tmp_path / "outside.jsonl"
    outside_target.write_text("{}\n", encoding="utf-8")

    traversal_id = "../../../../outside"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", traversal_id)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)

    path, how = discovery.find_current_transcript(cwd=cwd, home=tmp_path)

    assert path != outside_target
    assert "CLAUDE_CODE_SESSION_ID" not in how  # rejected -> fell through to fallback/none


def test_find_current_transcript_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    path, how = discovery.find_current_transcript(cwd="/nowhere", home=tmp_path)
    assert path is None
    assert how == "no transcript found"


def test_safe_mtime_returns_none_for_vanished_file(tmp_path):
    ghost = tmp_path / "gone.jsonl"
    assert discovery._safe_mtime(ghost) is None


def test_most_recent_transcript_tolerates_stat_race(tmp_path, monkeypatch):
    # Simulates a transcript rotated/deleted between rglob() yielding it and stat() being
    # called on it - a real race, not just a pre-existing-missing file. Must skip, not
    # crash the scan (the bug review-cli found in the mtime pre-filter path).
    from pathlib import Path as _Path

    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    real = root / "real.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    ghost = root / "ghost.jsonl"
    ghost.write_text("{}\n", encoding="utf-8")

    original_stat = _Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "ghost.jsonl":
            raise FileNotFoundError(self)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "stat", flaky_stat)

    found = discovery._most_recent_transcript(root)
    assert found == real


def test_discover_account_dirs_tolerates_permission_error_on_accounts_root(
    tmp_path, monkeypatch
):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    (tmp_path / ".claude-accounts").mkdir()

    from pathlib import Path as _Path

    original_iterdir = _Path.iterdir

    def denied_iterdir(self):
        if self == tmp_path / ".claude-accounts":
            raise PermissionError(self)
        return original_iterdir(self)

    monkeypatch.setattr(_Path, "iterdir", denied_iterdir, raising=True)

    dirs = discovery.discover_account_dirs(home=tmp_path)
    # The stock ~/.claude is still reported even though ~/.claude-accounts is unreadable.
    assert [d.name for d in dirs] == [".claude"]


def test_iter_jsonl_files_tolerates_unreadable_subdirectory(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    readable = root / "proj-a"
    readable.mkdir(parents=True)
    (readable / "s1.jsonl").write_text("{}\n", encoding="utf-8")
    blocked = root / "proj-b"
    blocked.mkdir()
    (blocked / "s2.jsonl").write_text("{}\n", encoding="utf-8")

    import os as _os

    original_walk = _os.walk

    def walk_with_denied_subdir(top, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in original_walk(top, onerror=onerror, **kwargs):
            if dirpath == str(blocked):
                if onerror:
                    onerror(PermissionError(dirpath))
                continue
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(_os, "walk", walk_with_denied_subdir)

    found = list(discovery._iter_jsonl_files(root))
    assert [p.name for p in found] == ["s1.jsonl"]


def test_iter_jsonl_files_excludes_fifo(tmp_path):
    import os as _os

    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    real = root / "real.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    fifo = root / "pipe.jsonl"
    _os.mkfifo(fifo)

    try:
        found = list(discovery._iter_jsonl_files(root))
    finally:
        fifo.unlink()

    assert [p.name for p in found] == ["real.jsonl"]


def test_iter_jsonl_files_excludes_symlinks(tmp_path):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    real = root / "real.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    link = root / "link.jsonl"
    link.symlink_to(real)

    found = list(discovery._iter_jsonl_files(root))

    assert [p.name for p in found] == ["real.jsonl"]


def test_find_transcripts_rejects_symlinked_projects_directory(tmp_path):
    # A symlinked ~/.claude/projects (or account-*/projects) must not be walked - it
    # could point anywhere on disk, defeating confinement to this account's own data.
    outside = tmp_path / "outside" / "p"
    outside.mkdir(parents=True)
    (outside / "secret.jsonl").write_text("{}\n", encoding="utf-8")

    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / "projects").symlink_to(outside.parent)

    assert discovery.find_transcripts(config_dir) == []


def test_discover_account_dirs_rejects_symlinked_stock_claude_dir(tmp_path):
    outside = tmp_path / "outside-claude"
    (outside / "projects").mkdir(parents=True)
    (tmp_path / ".claude").symlink_to(outside)

    assert discovery.discover_account_dirs(home=tmp_path) == []


def test_discover_account_dirs_rejects_symlinked_account_entry(tmp_path):
    outside = tmp_path / "outside-account"
    (outside / "projects").mkdir(parents=True)
    accounts_root = tmp_path / ".claude-accounts"
    accounts_root.mkdir()
    (accounts_root / "account-0").symlink_to(outside)

    assert discovery.discover_account_dirs(home=tmp_path) == []


def test_find_transcripts_respects_modified_after_filter(tmp_path):
    config_dir = tmp_path / ".claude"
    projects = config_dir / "projects" / "proj"
    projects.mkdir(parents=True)
    old = projects / "old.jsonl"
    new = projects / "new.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    new.write_text("{}\n", encoding="utf-8")

    old_time = time.time() - 3600 * 10
    os.utime(old, (old_time, old_time))

    all_files = discovery.find_transcripts(config_dir)
    assert {p.name for p in all_files} == {"old.jsonl", "new.jsonl"}

    recent_only = discovery.find_transcripts(config_dir, modified_after=time.time() - 3600)
    assert [p.name for p in recent_only] == ["new.jsonl"]


def test_find_transcripts_tolerates_stat_race_under_window_filter(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    config_dir = tmp_path / ".claude"
    projects = config_dir / "projects" / "proj"
    projects.mkdir(parents=True)
    real = projects / "real.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    ghost = projects / "ghost.jsonl"
    ghost.write_text("{}\n", encoding="utf-8")

    original_stat = _Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "ghost.jsonl":
            raise FileNotFoundError(self)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "stat", flaky_stat)

    found = discovery.find_transcripts(config_dir, modified_after=time.time() - 3600)
    assert [p.name for p in found] == ["real.jsonl"]
