from __future__ import annotations

import json

import pytest

from harness_cli.transcripts import parse_transcript

# Line shapes below mirror what was observed directly in real Claude Code transcripts
# (~/.claude-accounts/account-0/projects/**/*.jsonl) during development - not guessed.

USER_LINE = {
    "type": "user",
    "timestamp": "2026-09-07T10:20:23.336Z",
    "sessionId": "abc123",
    "message": {"role": "user", "content": "hello"},
}

ASSISTANT_LINE = {
    "type": "assistant",
    "timestamp": "2026-09-07T10:20:30.648Z",
    "sessionId": "abc123",
    "message": {
        "role": "assistant",
        "model": "claude-sonnet-5",
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 314,
            "cache_read_input_tokens": 164487,
            "output_tokens": 100,
        },
    },
}

ATTACHMENT_LINE = {"type": "attachment", "timestamp": "2026-09-07T10:20:23.338Z"}

ERROR_LINE = {
    "type": "assistant",
    "timestamp": "2026-09-07T10:21:00.000Z",
    "isApiErrorMessage": True,
    "message": {
        "model": "<synthetic>",
        "role": "assistant",
        "content": [{"type": "text", "text": "Not logged in · Please run /login"}],
    },
}


def _write_jsonl(path, lines):
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def test_parse_transcript_sums_real_usage_fields(tmp_path):
    path = tmp_path / "abc123.jsonl"
    _write_jsonl(path, [USER_LINE, ATTACHMENT_LINE, ASSISTANT_LINE])

    session = parse_transcript(path, account="default")

    assert session.session_id == "abc123"
    assert session.account == "default"
    assert session.message_count == 2  # user + assistant, not the attachment line
    assert session.models == {"claude-sonnet-5"}
    assert session.totals.input_tokens == 2
    assert session.totals.output_tokens == 100
    assert session.totals.cache_creation_input_tokens == 314
    assert session.totals.cache_read_input_tokens == 164487
    assert session.first_timestamp == "2026-09-07T10:20:23.336Z"
    assert session.last_timestamp == "2026-09-07T10:20:30.648Z"


def test_parse_transcript_records_api_error_events(tmp_path):
    path = tmp_path / "abc123.jsonl"
    _write_jsonl(path, [USER_LINE, ERROR_LINE])

    session = parse_transcript(path, account="default")

    assert len(session.error_events) == 1
    event = session.error_events[0]
    assert event.category == "auth"
    assert "Please run /login" in event.text
    assert event.reset_hint is None
    # A synthetic error message must never be counted as a real model usage sample.
    assert "<synthetic>" not in session.models
    assert session.totals.assistant_turns == 0


SESSION_LIMIT_LINE = {
    "type": "assistant",
    "timestamp": "2026-08-13T16:40:23.696Z",
    "isApiErrorMessage": True,
    "message": {
        "model": "<synthetic>",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "You've hit your session limit · resets 7:30pm (Europe/Belgrade)"}
        ],
    },
}


def test_parse_transcript_extracts_reset_hint_from_session_limit_error(tmp_path):
    path = tmp_path / "abc123.jsonl"
    _write_jsonl(path, [SESSION_LIMIT_LINE])

    session = parse_transcript(path, account="default")

    assert len(session.error_events) == 1
    event = session.error_events[0]
    assert event.category == "session_limit"
    assert event.reset_hint == "7:30pm (Europe/Belgrade)"


def test_parse_transcript_skips_malformed_lines(tmp_path):
    path = tmp_path / "abc123.jsonl"
    path.write_text(
        json.dumps(USER_LINE) + "\n" + "{not valid json\n" + json.dumps(ASSISTANT_LINE) + "\n",
        encoding="utf-8",
    )

    session = parse_transcript(path, account="default")

    assert session.message_count == 2
    assert session.totals.assistant_turns == 1


def test_parse_transcript_skips_non_dict_json_lines(tmp_path):
    # A syntactically valid JSON line that isn't an object (list/string/null) is not a
    # shape Claude Code itself writes - skip it, don't crash on entry.get(...).
    path = tmp_path / "abc123.jsonl"
    path.write_text(
        "[]\n" + '"just a string"\n' + "null\n" + json.dumps(ASSISTANT_LINE) + "\n",
        encoding="utf-8",
    )

    session = parse_transcript(path, account="default")

    assert session.totals.assistant_turns == 1
    assert session.totals.output_tokens == 100


def test_parse_transcript_only_message_entries_move_last_timestamp(tmp_path):
    # A fresh "attachment"-type record must not extend last_timestamp past the real last
    # user/assistant message - it isn't message content, just metadata.
    path = tmp_path / "abc123.jsonl"
    stale_assistant = dict(ASSISTANT_LINE, timestamp="2020-01-01T00:00:00.000Z")
    fresh_attachment = dict(ATTACHMENT_LINE, timestamp="2026-09-07T12:00:00.000Z")
    _write_jsonl(path, [stale_assistant, fresh_attachment])

    session = parse_transcript(path, account="default")

    assert session.last_timestamp == "2020-01-01T00:00:00.000Z"


def test_parse_transcript_ignores_malformed_string_timestamp(tmp_path):
    # A corrupted-but-still-a-string timestamp (e.g. "zzzz") must not become
    # last_timestamp itself - that would defeat cli.py's --window-hours staleness check,
    # which can't parse "zzzz" either and would then conservatively KEEP the stale
    # session forever (parse_iso_timestamp returns None -> "unknown, don't drop it").
    path = tmp_path / "abc123.jsonl"
    old_valid = dict(ASSISTANT_LINE, timestamp="2020-01-01T00:00:00.000Z")
    garbage = dict(ASSISTANT_LINE, timestamp="zzzz")
    _write_jsonl(path, [old_valid, garbage])

    session = parse_transcript(path, account="default")

    assert session.last_timestamp == "2020-01-01T00:00:00.000Z"


def test_parse_transcript_ignores_non_string_timestamp(tmp_path):
    # A numeric (epoch) timestamp isn't the string shape Claude Code itself writes;
    # storing it as-is would later crash timeutil.parse_iso_timestamp's .endswith() call.
    path = tmp_path / "abc123.jsonl"
    bad_ts_line = dict(ASSISTANT_LINE, timestamp=1700000000)
    _write_jsonl(path, [bad_ts_line])

    session = parse_transcript(path, account="default")

    assert session.last_timestamp is None
    assert session.totals.output_tokens == 100  # usage itself still parses fine


def test_parse_transcript_ignores_non_string_model(tmp_path):
    # A spoofed/malformed "model" field (list/dict/number) isn't a real model name -
    # session.models is a set, so adding a list would crash with an unhashable-type error.
    path = tmp_path / "abc123.jsonl"
    bad_model_line = {
        "type": "assistant",
        "timestamp": "2026-09-07T10:20:30.648Z",
        "message": {"model": ["spoof"], "usage": {"output_tokens": 1}},
    }
    _write_jsonl(path, [bad_model_line])

    session = parse_transcript(path, account="default")

    assert session.models == set()
    assert session.totals.output_tokens == 1  # usage itself is unaffected


def test_parse_transcript_ignores_non_string_error_text_block(tmp_path):
    path = tmp_path / "abc123.jsonl"
    bad_error_line = {
        "type": "assistant",
        "timestamp": "2026-09-07T10:21:00.000Z",
        "isApiErrorMessage": True,
        "message": {"content": [{"type": "text", "text": 7}]},
    }
    _write_jsonl(path, [bad_error_line])

    session = parse_transcript(path, account="default")

    assert len(session.error_events) == 1
    assert session.error_events[0].text == ""
    assert session.error_events[0].category == "other"


def test_parse_transcript_ignores_usage_on_user_records(tmp_path):
    # "assistant turns with usage data" (report.py's own label) must mean assistant
    # records only - a user record carrying its own `message.usage` must not count.
    path = tmp_path / "abc123.jsonl"
    user_with_usage = {
        "type": "user",
        "timestamp": "2026-09-07T10:20:00.000Z",
        "message": {"usage": {"output_tokens": 999}},
    }
    _write_jsonl(path, [user_with_usage])

    session = parse_transcript(path, account="default")

    assert session.totals.assistant_turns == 0
    assert session.totals.output_tokens == 0
    assert session.message_count == 1  # still counted as a message, just not as usage


def test_parse_transcript_tracks_max_timestamp_even_if_written_out_of_order(tmp_path):
    # A transcript isn't guaranteed strict chronological order (merge/restore/retry can
    # append an older record after a newer one) - last_timestamp must be the max VALUE
    # seen, not just whatever the last line in the file happens to say.
    path = tmp_path / "abc123.jsonl"
    newer = dict(ASSISTANT_LINE, timestamp="2026-09-07T12:00:00.000Z")
    older_written_last = dict(ASSISTANT_LINE, timestamp="2026-09-01T00:00:00.000Z")
    _write_jsonl(path, [newer, older_written_last])

    session = parse_transcript(path, account="default")

    assert session.last_timestamp == "2026-09-07T12:00:00.000Z"
    assert session.first_timestamp == "2026-09-01T00:00:00.000Z"


def test_parse_transcript_compares_timestamps_as_instants_not_strings(tmp_path):
    # "2026-09-07T12:57:28+14:00" reads as lexically LARGER than "2026-09-07T11:57:28Z"
    # (raw-string comparison sees "12" > "11"), but +14:00 is 14 hours ahead of UTC, so its
    # real instant is 2026-09-06T22:57:28Z - actually EARLIER than the fresh Z timestamp.
    # A naive string-min/max would wrongly pick the offset one as "most recent".
    path = tmp_path / "abc123.jsonl"
    fresh = dict(ASSISTANT_LINE, timestamp="2026-09-07T11:57:28.000Z")
    lexically_larger_but_actually_earlier = dict(
        ASSISTANT_LINE, timestamp="2026-09-07T12:57:28.000+14:00"
    )
    _write_jsonl(path, [fresh, lexically_larger_but_actually_earlier])

    session = parse_transcript(path, account="default")

    assert session.last_timestamp == "2026-09-07T11:57:28.000Z"


def test_parse_transcript_skips_line_with_oversized_integer_literal(tmp_path):
    # json.loads raises ValueError (not JSONDecodeError) for an integer literal past
    # Python's default digit-conversion limit - a malformed line, still just to be
    # skipped, not a reason to abort the whole scan.
    path = tmp_path / "abc123.jsonl"
    huge_int = "9" * 4400  # past Python's default 4300-digit int-string-conversion limit
    bad_line = (
        '{"type":"assistant","timestamp":"2026-09-07T10:00:00.000Z",'
        f'"message":{{"model":"claude-sonnet-5","usage":{{"output_tokens":{huge_int}}}}}}}'
    )
    path.write_text(bad_line + "\n" + json.dumps(ASSISTANT_LINE) + "\n", encoding="utf-8")

    session = parse_transcript(path, account="default")

    assert session.totals.assistant_turns == 1
    assert session.totals.output_tokens == 100


def test_parse_transcript_refuses_to_open_a_fifo(tmp_path):
    # A named pipe named "*.jsonl" (spoofed or corrupted tree) must be rejected up front,
    # not handed to open() - opening a FIFO with no writer on the other end blocks
    # forever, hanging the whole CLI.
    import os as _os

    path = tmp_path / "not-really-a-file.jsonl"
    _os.mkfifo(path)
    try:
        with pytest.raises(OSError):
            parse_transcript(path, account="default")
    finally:
        path.unlink()


def test_parse_transcript_refuses_to_open_a_symlink(tmp_path):
    # is_file() FOLLOWS symlinks - a "*.jsonl" symlink into another account's
    # transcripts (or outside the projects tree) must not be scanned as if it were a
    # real transcript belonging here (double-counting / confinement bypass).
    real = tmp_path / "real.jsonl"
    _write_jsonl(real, [ASSISTANT_LINE])
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)

    with pytest.raises(OSError):
        parse_transcript(link, account="default")


def test_parse_transcript_skips_pathologically_nested_json_line(tmp_path):
    # Thousands of nested arrays is syntactically valid JSON but can exceed Python's
    # call-stack depth inside the decoder itself (RecursionError, not JSONDecodeError) -
    # still just a malformed line to skip, not a reason to abort the whole scan.
    path = tmp_path / "abc123.jsonl"
    nested = "[" * 100000 + "]" * 100000
    path.write_text(nested + "\n" + json.dumps(ASSISTANT_LINE) + "\n", encoding="utf-8")

    session = parse_transcript(path, account="default")

    assert session.totals.assistant_turns == 1
    assert session.totals.output_tokens == 100


def test_parse_transcript_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    session = parse_transcript(path, account="default")

    assert session.message_count == 0
    assert session.totals.total_tokens == 0
    assert session.error_events == []
