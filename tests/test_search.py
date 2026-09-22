from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness_cli import cli


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


def _cc_user(when: datetime, content, **flags) -> dict:
    entry = {
        "type": "user",
        "timestamp": _iso(when),
        "message": {"role": "user", "content": content},
    }
    entry.update(flags)
    return entry


def _omp_user(when: datetime, text: str, attribution: str = "user") -> dict:
    return {
        "type": "message",
        "timestamp": _iso(when),
        "message": {
            "role": "user",
            "attribution": attribution,
            "content": [{"type": "text", "text": text}],
        },
    }


def test_search_finds_cc_parent_user_ignores_assistant(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    path = tmp_path / ".claude" / "projects" / "-proj" / "sess.jsonl"
    _write_jsonl(
        path,
        [
            _cc_user(now, "find the needle here"),
            {
                "type": "assistant",
                "timestamp": _iso(now + timedelta(seconds=1)),
                "message": {"role": "assistant", "content": "needle in assistant reply"},
            },
        ],
    )

    rc = cli.main(["search", "NeEdLe"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "cc" in out
    assert str(path) in out
    assert "find the needle here" in out
    assert _iso(now) in out
    assert "needle in assistant reply" not in out


def test_search_finds_omp_parent_user_text_block(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    path = tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "sess.jsonl"
    _write_jsonl(path, [_omp_user(now, "omp needle text")])

    rc = cli.main(["search", "omp needle"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "omp" in out
    assert str(path) in out
    assert "omp needle text" in out


def test_search_skips_nested_subagent_jsonl(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    omp_nested = tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "sess" / "HarnessSearch.jsonl"
    cc_nested = (
        tmp_path / ".claude" / "projects" / "-proj" / "sess" / "subagents" / "agent-abc.jsonl"
    )
    _write_jsonl(omp_nested, [_omp_user(now, "hidden needle")])
    _write_jsonl(cc_nested, [_cc_user(now, "hidden needle")])

    rc = cli.main(["search", "hidden needle"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "No matching user messages." in out
    assert "hidden needle" not in out


def test_search_skips_cc_meta_sidechain_compact_tool_result(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    path = tmp_path / ".claude" / "projects" / "-proj" / "sess.jsonl"
    tool_turn = [
        {"type": "tool_result", "content": "needle tool"},
        {"type": "text", "text": "needle in tool_result turn"},
    ]
    _write_jsonl(
        path,
        [
            _cc_user(now, "needle meta", isMeta=True),
            _cc_user(now, "needle sidechain", isSidechain=True),
            _cc_user(now, "needle compact", isCompactSummary=True),
            _cc_user(now, tool_turn),
            _cc_user(now, "real user needle kept"),
        ],
    )

    rc = cli.main(["search", "needle"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "real user needle kept" in out
    assert "needle meta" not in out
    assert "needle sidechain" not in out
    assert "needle compact" not in out
    assert "needle in tool_result turn" not in out


def test_search_skips_omp_agent_attribution(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    path = tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "sess.jsonl"
    _write_jsonl(
        path,
        [
            _omp_user(now, "Complete assignment thoroughly needle", attribution="agent"),
            _omp_user(now, "real omp user needle", attribution="user"),
        ],
    )

    rc = cli.main(["search", "needle"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "real omp user needle" in out
    assert "Complete assignment thoroughly" not in out


def test_search_days_filter_drops_old_keeps_fresh(tmp_path, monkeypatch, capsys):
    # Files are written now (fresh mtime) so a pass here proves the filter uses the
    # message timestamp, not just filesystem mtime.
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    slug = tmp_path / ".claude" / "projects" / "-proj"
    _write_jsonl(
        slug / "old.jsonl",
        [_cc_user(now - timedelta(days=10), "old needle")],
    )
    _write_jsonl(slug / "new.jsonl", [_cc_user(now, "fresh needle")])

    rc = cli.main(["search", "needle", "--days", "3"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "fresh needle" in out
    assert "old needle" not in out


def test_top_level_help_lists_search(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "search" in out


def test_build_transcript_corpus_indexes_cc_and_omp_user_text(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    cc_path = tmp_path / ".claude" / "projects" / "-proj" / "sess.jsonl"
    _write_jsonl(cc_path, [_cc_user(now, "working on ticket-42 things")])
    omp_path = tmp_path / ".omp" / "agent" / "sessions" / "-proj" / "sess.jsonl"
    _write_jsonl(omp_path, [_omp_user(now, "omp session mentions #7")])

    from harness_cli.search import build_transcript_corpus, paths_matching_id, paths_matching_text

    corpus = build_transcript_corpus()

    assert cc_path in corpus
    assert "ticket-42" in corpus[cc_path].text
    assert omp_path in corpus
    assert "#7" in corpus[omp_path].id_tokens
    assert paths_matching_text(corpus, "ticket-42") == [cc_path]
    assert paths_matching_id(corpus, "7") == [omp_path]
    assert paths_matching_id(corpus, "21") == []
    assert paths_matching_text(corpus, "no such needle") == []


def test_paths_matching_id_never_matches_a_longer_number(tmp_path, monkeypatch):
    # "#2" must never match a file that only mentions "#21"/"#200" - a real review finding.
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    path = tmp_path / ".claude" / "projects" / "-proj" / "sess.jsonl"
    _write_jsonl(path, [_cc_user(now, "fixed #21 and #200 today")])

    from harness_cli.search import build_transcript_corpus, paths_matching_id

    corpus = build_transcript_corpus()

    assert paths_matching_id(corpus, "2") == []
    assert paths_matching_id(corpus, "21") == [path]
    assert paths_matching_id(corpus, "200") == [path]


def test_build_transcript_corpus_excludes_assistant_and_nested_subagent(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    path = tmp_path / ".claude" / "projects" / "-proj" / "sess.jsonl"
    _write_jsonl(
        path,
        [
            _cc_user(now, "real user needle"),
            {
                "type": "assistant",
                "timestamp": _iso(now + timedelta(seconds=1)),
                "message": {"role": "assistant", "content": "assistant needle"},
            },
        ],
    )
    nested_path = (
        tmp_path / ".claude" / "projects" / "-proj" / "sess" / "subagents" / "agent-abc.jsonl"
    )
    _write_jsonl(nested_path, [_cc_user(now, "hidden nested needle")])

    from harness_cli.search import build_transcript_corpus

    corpus = build_transcript_corpus()

    assert "real user needle" in corpus[path].text
    assert "assistant needle" not in corpus[path].text
    assert nested_path not in corpus
