from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness_cli import cli, stats


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)


def _write_jsonl(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _cc_user(when: datetime, content) -> dict:
    return {
        "type": "user",
        "timestamp": _iso(when),
        "sessionId": "abc123",
        "message": {"role": "user", "content": content},
    }


def _cc_assistant(when: datetime, usage: dict) -> dict:
    return {
        "type": "assistant",
        "timestamp": _iso(when),
        "sessionId": "abc123",
        "message": {"role": "assistant", "model": "claude-sonnet-5", "usage": usage},
    }


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _json_proc(payload) -> _FakeProc:
    return _FakeProc(returncode=0, stdout=json.dumps(payload))


def _dispatcher(monkeypatch, responses: dict):
    """Install a fake `subprocess.run` on `harness_cli.stats.subprocess` keyed by exact argv
    tuple. A response may be a `_FakeProc` or an `Exception` instance (raised instead)."""

    def _run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in responses:
            raise AssertionError(f"unexpected subprocess call: {cmd}")
        value = responses[key]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(stats.subprocess, "run", _run)


# ── ticket-reference resolution ──────────────────────────────────────────────────────


def test_resolve_ticket_explicit_repo_hash_id():
    repo, ticket_id = stats.resolve_ticket("acme/widgets#42", None)
    assert (repo, ticket_id) == ("acme/widgets", "42")


def test_resolve_ticket_bare_id_uses_repo_flag():
    repo, ticket_id = stats.resolve_ticket("42", "acme/widgets")
    assert (repo, ticket_id) == ("acme/widgets", "42")


def test_resolve_ticket_bare_id_without_repo_or_git_remote_raises(tmp_path):
    with pytest.raises(stats.StatsError, match="has no repo"):
        stats.resolve_ticket("42", None, cwd=tmp_path)


def test_resolve_ticket_malformed_ref_raises():
    with pytest.raises(stats.StatsError, match="cannot parse"):
        stats.resolve_ticket("not-a-ticket", None)


def test_parse_git_remote_url_https_and_ssh():
    assert stats._parse_git_remote_url("https://forge.example.com/acme/widgets.git") == (
        "acme/widgets"
    )
    assert stats._parse_git_remote_url("git@forge.example.com:acme/widgets.git") == ("acme/widgets")


# ── happy path: followups + review iterations + token match ─────────────────────────


def test_happy_path_followup_review_iterations_and_tokens(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "42", "--repo", repo, "--json"): _json_proc(
            {
                "id": "42",
                "title": "Add widget export",
                "state": "done",
                "links": {"Followed up by #43": "https://example/43"},
            }
        ),
        ("task", "read", "43", "--repo", repo, "--json"): _json_proc(
            {"id": "43", "title": "Fix widget export regression", "state": "todo", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "42 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([{"number": 7}])
        ),
        ("gh", "pr", "view", "7", "--repo", repo, "--json", "reviews"): _json_proc(
            {
                "reviews": [
                    {"state": "CHANGES_REQUESTED"},
                    {"state": "COMMENTED"},
                    {"state": "CHANGES_REQUESTED"},
                    {"state": "APPROVED"},
                ],
                "commits": [{"oid": "abc"}],
            }
        ),
    }
    _dispatcher(monkeypatch, responses)

    now = datetime.now(timezone.utc)
    _write_jsonl(
        tmp_path / ".claude" / "projects" / "-proj" / "sess.jsonl",
        [
            _cc_user(now, "working on #42 add widget export"),
            _cc_assistant(
                now,
                {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            ),
        ],
    )

    result = stats.build_ticket_stats("42", repo)

    assert result.ticket_id == "acme/widgets#42"
    assert result.title == "Add widget export"
    assert result.state == "done"
    assert result.review_iterations == 2
    assert result.review_iterations_reason is None
    assert result.followups == [
        {"id": "acme/widgets#43", "title": "Fix widget export regression", "state": "todo"}
    ]
    assert result.followups_unavailable_reason is None
    assert result.tokens == {"total": 30, "sessions": 1}
    assert result.tokens_unavailable_reason is None


# ── no PR found ───────────────────────────────────────────────────────────────────────


def test_no_pr_found_leaves_review_iterations_none_with_reason(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "9", "--repo", repo, "--json"): _json_proc(
            {"id": "9", "title": "Something obscure", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "9 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([])
        ),
    }
    _dispatcher(monkeypatch, responses)

    result = stats.build_ticket_stats("9", repo)

    assert result.review_iterations is None
    assert result.review_iterations_reason == "no PR found referencing this ticket"
    assert result.followups == []
    assert result.followups_unavailable_reason is None


# ── no local transcript match ────────────────────────────────────────────────────────


def test_no_transcript_match_leaves_tokens_none_with_reason(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "9", "--repo", repo, "--json"): _json_proc(
            {"id": "9", "title": "Something obscure", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "9 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([])
        ),
    }
    _dispatcher(monkeypatch, responses)

    result = stats.build_ticket_stats("9", repo)

    assert result.tokens is None
    assert result.tokens_unavailable_reason == "no local transcript match"


# ── missing binaries degrade cleanly ─────────────────────────────────────────────────


def test_missing_task_binary_degrades_cleanly_not_a_crash(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "42", "--repo", repo, "--json"): FileNotFoundError(),
    }
    _dispatcher(monkeypatch, responses)

    result = stats.build_ticket_stats("42", repo)

    assert result.title == ""
    assert result.state == ""
    assert result.review_iterations is None
    assert "task" in result.review_iterations_reason
    assert "not found on PATH" in result.review_iterations_reason
    assert result.followups == []
    assert result.followups_unavailable_reason is not None
    assert result.tokens is None
    assert result.tokens_unavailable_reason is not None


def test_missing_gh_binary_degrades_cleanly_not_a_crash(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "42", "--repo", repo, "--json"): _json_proc(
            {"id": "42", "title": "Add widget export", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "42 in:body", "--repo", repo, "--json", "number"): (
            FileNotFoundError()
        ),
    }
    _dispatcher(monkeypatch, responses)

    result = stats.build_ticket_stats("42", repo)

    assert result.title == "Add widget export"
    assert result.review_iterations is None
    assert "gh" in result.review_iterations_reason
    assert "not found on PATH" in result.review_iterations_reason
    # followups came back genuinely empty (no `Followed up by` link), not an error path.
    assert result.followups == []
    assert result.followups_unavailable_reason is None


def test_permission_error_on_binary_degrades_cleanly_not_a_crash(tmp_path, monkeypatch):
    # A `PermissionError` (binary exists but isn't executable) is a SIBLING of
    # FileNotFoundError under OSError, not a subclass of it - a narrower except clause would
    # let this escape uncaught.
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "42", "--repo", repo, "--json"): PermissionError("not executable"),
    }
    _dispatcher(monkeypatch, responses)

    result = stats.build_ticket_stats("42", repo)

    assert result.title == ""
    assert result.review_iterations is None
    assert "could not be run" in result.review_iterations_reason


# ── --all mode aggregate summary ─────────────────────────────────────────────────────


def test_all_mode_aggregate_summary(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        (
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,title,state",
            "--limit",
            "200",
        ): _json_proc(
            [
                {"number": 1, "title": "A", "state": "CLOSED"},
                {"number": 2, "title": "B", "state": "CLOSED"},
            ]
        ),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--json",
            "number,body,reviews",
            "--limit",
            "1000",
        ): _json_proc(
            [{"number": 11, "body": "Refs #1", "reviews": [{"state": "CHANGES_REQUESTED"}]}]
        ),
        ("task", "read", "1", "--repo", repo, "--json"): _json_proc(
            {
                "id": "1",
                "title": "Ticket A",
                "state": "done",
                "links": {"Followed up by #99": "url"},
            }
        ),
        ("task", "read", "99", "--repo", repo, "--json"): _json_proc(
            {"id": "99", "title": "Followup of A", "state": "todo", "links": {}}
        ),
        ("task", "read", "2", "--repo", repo, "--json"): _json_proc(
            {"id": "2", "title": "Ticket B", "state": "done", "links": {}}
        ),
    }
    _dispatcher(monkeypatch, responses)

    results, summary, reason = stats.build_all_stats(repo)

    assert reason is None
    assert len(results) == 2
    assert summary["total_tickets"] == 2
    assert summary["review_iteration_buckets"] == {
        "0": 0,
        "1": 1,
        "2": 0,
        "3+": 0,
        "unknown": 1,
    }
    assert summary["with_followups"] == 1
    assert summary["with_tokens"] == 0
    assert summary["truncated"] is False


def test_prs_matching_uses_word_boundary_not_raw_substring():
    # Ticket "2" must NOT match a PR body that merely contains "2" as part of a bigger number
    # ("#21") - a raw `in` substring check would over-match and inflate review-iteration counts.
    prs = [
        {"number": 1, "body": "Refs #21", "reviews": [{"state": "CHANGES_REQUESTED"}]},
        {"number": 2, "body": "Refs #2", "reviews": [{"state": "CHANGES_REQUESTED"}]},
    ]
    matched = stats._prs_matching(prs, "2")
    assert [pr["number"] for pr in matched] == [2]


def test_all_mode_builds_transcript_corpus_exactly_once(tmp_path, monkeypatch):
    # Regression for a review finding pinned across three rounds: `collect_token_usage` must
    # reuse ONE corpus scan across every ticket in an --all run, never rescan per ticket/needle.
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        (
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,title,state",
            "--limit",
            "200",
        ): _json_proc(
            [
                {"number": 1, "title": "A", "state": "CLOSED"},
                {"number": 2, "title": "B", "state": "CLOSED"},
            ]
        ),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--json",
            "number,body,reviews",
            "--limit",
            "1000",
        ): _json_proc([]),
        ("task", "read", "1", "--repo", repo, "--json"): _json_proc(
            {"id": "1", "title": "Ticket A", "state": "done", "links": {}}
        ),
        ("task", "read", "2", "--repo", repo, "--json"): _json_proc(
            {"id": "2", "title": "Ticket B", "state": "done", "links": {}}
        ),
    }
    _dispatcher(monkeypatch, responses)

    calls = []
    from harness_cli import search as search_module

    real_build = search_module.build_transcript_corpus

    def _counting_build():
        calls.append(1)
        return real_build()

    monkeypatch.setattr(search_module, "build_transcript_corpus", _counting_build)

    stats.build_all_stats(repo)

    assert len(calls) == 1


def test_all_mode_falls_back_to_per_ticket_search_when_bulk_pr_fetch_fails(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        (
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,title,state",
            "--limit",
            "200",
        ): _json_proc([{"number": 5, "title": "E", "state": "CLOSED"}]),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--json",
            "number,body,reviews",
            "--limit",
            "1000",
        ): FileNotFoundError(),
        ("task", "read", "5", "--repo", repo, "--json"): _json_proc(
            {"id": "5", "title": "Ticket E", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "5 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([{"number": 55}])
        ),
        ("gh", "pr", "view", "55", "--repo", repo, "--json", "reviews"): _json_proc(
            {"reviews": [{"state": "CHANGES_REQUESTED"}]}
        ),
    }
    _dispatcher(monkeypatch, responses)

    results, summary, reason = stats.build_all_stats(repo)

    assert reason is None
    assert len(results) == 1
    assert results[0].review_iterations == 1
    assert summary["prs_truncated"] is False


def test_all_mode_falls_back_when_bulk_pr_fetch_times_out(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        (
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,title,state",
            "--limit",
            "200",
        ): _json_proc([{"number": 5, "title": "E", "state": "CLOSED"}]),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--json",
            "number,body,reviews",
            "--limit",
            "1000",
        ): subprocess.TimeoutExpired(cmd="gh", timeout=90.0),
        ("task", "read", "5", "--repo", repo, "--json"): _json_proc(
            {"id": "5", "title": "Ticket E", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "5 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([{"number": 55}])
        ),
        ("gh", "pr", "view", "55", "--repo", repo, "--json", "reviews"): _json_proc(
            {"reviews": [{"state": "CHANGES_REQUESTED"}]}
        ),
    }
    _dispatcher(monkeypatch, responses)

    results, _summary, reason = stats.build_all_stats(repo)

    assert reason is None
    assert len(results) == 1
    assert results[0].review_iterations == 1


def test_all_mode_pr_list_truncation_is_signaled_not_silent(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        (
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,title,state",
            "--limit",
            "200",
        ): _json_proc([{"number": 1, "title": "A", "state": "CLOSED"}]),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--json",
            "number,body,reviews",
            "--limit",
            "1000",
        ): _json_proc([{"number": n, "body": "", "reviews": []} for n in range(1000)]),
        ("task", "read", "1", "--repo", repo, "--json"): _json_proc(
            {"id": "1", "title": "Ticket A", "state": "done", "links": {}}
        ),
    }
    _dispatcher(monkeypatch, responses)

    results, summary, reason = stats.build_all_stats(repo)

    assert reason is None
    assert summary["prs_truncated"] is True
    human = stats.render_all(results, summary)
    assert "gh pr list capped" in human


def test_collect_followups_partial_failure_lists_successes_with_reason():
    repo = "acme/widgets"
    links = {"Followed up by #10": "url", "Followed up by #11": "url"}

    def fake_run(cmd, **kw):
        class P:
            def __init__(self, rc, out, err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        if cmd == ["task", "read", "10", "--repo", repo, "--json"]:
            return P(0, json.dumps({"id": "10", "title": "Follow-up ten", "state": "todo"}))
        if cmd == ["task", "read", "11", "--repo", repo, "--json"]:
            return P(1, "", "not found")
        raise AssertionError(cmd)

    orig = stats.subprocess.run
    stats.subprocess.run = fake_run
    try:
        followups, reason = stats.collect_followups(repo, links)
    finally:
        stats.subprocess.run = orig

    assert followups == [{"id": "acme/widgets#10", "title": "Follow-up ten", "state": "todo"}]
    assert reason is not None
    assert "#11" in reason


def test_parse_transcript_cached_memoizes_across_calls(tmp_path):
    import threading as _threading

    from harness_cli.stats import _parse_transcript_cached

    path = tmp_path / "sess.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        ],
    )
    cache: dict = {}
    lock = _threading.Lock()

    first = _parse_transcript_cached(path, cache, lock)
    path.unlink()  # a second real parse would now raise/return None - proves the cache is used
    second = _parse_transcript_cached(path, cache, lock)

    assert first is second
    assert second.totals.total_tokens == 8


def test_all_mode_truncation_at_limit_is_signaled_not_silent(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        (
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,title,state",
            "--limit",
            "200",
        ): _json_proc([{"number": n, "title": f"T{n}", "state": "CLOSED"} for n in range(1, 201)]),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--json",
            "number,body,reviews",
            "--limit",
            "1000",
        ): _json_proc([]),
    }
    for n in range(1, 201):
        responses[("task", "read", str(n), "--repo", repo, "--json")] = _json_proc(
            {"id": str(n), "title": f"T{n}", "state": "done", "links": {}}
        )
    _dispatcher(monkeypatch, responses)

    results, summary, reason = stats.build_all_stats(repo)

    assert reason is None
    assert len(results) == 200
    assert summary["truncated"] is True
    human = stats.render_all(results, summary)
    assert "may have more" in human


def test_review_iterations_partial_failure_reason_is_not_silently_dropped(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "42", "--repo", repo, "--json"): _json_proc(
            {"id": "42", "title": "Add widget export", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "42 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([{"number": 7}, {"number": 8}])
        ),
        ("gh", "pr", "view", "7", "--repo", repo, "--json", "reviews"): _json_proc(
            {"reviews": [{"state": "CHANGES_REQUESTED"}]}
        ),
        ("gh", "pr", "view", "8", "--repo", repo, "--json", "reviews"): _FakeProc(
            returncode=1, stderr="rate limited"
        ),
    }
    _dispatcher(monkeypatch, responses)

    result = stats.build_ticket_stats("42", repo)

    # The count from PR #7 is real and must not be discarded, but it must ALSO not present as
    # a confident, complete number when PR #8's reviews could not be loaded.
    assert result.review_iterations == 1
    assert result.review_iterations_reason is not None
    assert "PR #8" in result.review_iterations_reason
    human = stats.render_stats(result)
    assert "review iterations: 1 (" in human


# ── --json output shape via CLI ──────────────────────────────────────────────────────


def test_cli_json_output_shape(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "42", "--repo", repo, "--json"): _json_proc(
            {"id": "42", "title": "Add widget export", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "42 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([])
        ),
    }
    _dispatcher(monkeypatch, responses)

    rc = cli.main(["stats", f"{repo}#42", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out)
    assert payload == {
        "ticket_id": "acme/widgets#42",
        "title": "Add widget export",
        "state": "done",
        "review_iterations": None,
        "review_iterations_reason": "no PR found referencing this ticket",
        "followups": [],
        "followups_unavailable_reason": None,
        "tokens": None,
        "tokens_unavailable_reason": "no local transcript match",
    }


def test_cli_human_output_for_ticket(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    repo = "acme/widgets"
    responses = {
        ("task", "read", "42", "--repo", repo, "--json"): _json_proc(
            {"id": "42", "title": "Add widget export", "state": "done", "links": {}}
        ),
        ("gh", "pr", "list", "--search", "42 in:body", "--repo", repo, "--json", "number"): (
            _json_proc([])
        ),
    }
    _dispatcher(monkeypatch, responses)

    rc = cli.main(["stats", f"{repo}#42"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "acme/widgets#42" in out
    assert "Add widget export" in out
    assert "review iterations: unknown (no PR found referencing this ticket)" in out
    assert "follow-ups: none" in out
    assert "tokens: unknown (no local transcript match)" in out


def test_cli_all_mode_requires_repo_when_no_git_remote(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stats, "_git_remote_repo", lambda cwd: None)

    rc = cli.main(["stats", "--all"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "--repo" in err


# ── help / dispatch wiring ───────────────────────────────────────────────────────────


def test_top_level_help_lists_stats(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stats" in out


def test_stats_help_lists_flags(capsys):
    with pytest.raises(SystemExit):
        cli.main(["stats", "--help"])
    help_text = capsys.readouterr().out
    assert "--repo" in help_text
    assert "--all" in help_text
    assert "--json" in help_text


def test_stats_module_not_imported_by_build_parser():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
        "import harness_cli.cli\n"
        "harness_cli.cli.build_parser()\n"
        "assert 'harness_cli.stats' not in sys.modules, sys.modules.keys()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
