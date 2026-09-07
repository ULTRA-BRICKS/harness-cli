from __future__ import annotations

from datetime import timezone

from harness_cli.timeutil import parse_iso_timestamp


def test_parse_iso_timestamp_z_suffix():
    dt = parse_iso_timestamp("2026-09-07T10:20:30.648Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026 and dt.month == 9 and dt.day == 7


def test_parse_iso_timestamp_none():
    assert parse_iso_timestamp(None) is None


def test_parse_iso_timestamp_empty_string():
    assert parse_iso_timestamp("") is None


def test_parse_iso_timestamp_malformed_string():
    assert parse_iso_timestamp("not a timestamp") is None


def test_parse_iso_timestamp_non_string_does_not_crash():
    # A malformed transcript could carry a numeric epoch instead of a string; must not
    # raise (regression: .endswith() on a non-str would crash the whole scan).
    assert parse_iso_timestamp(1700000000) is None
    assert parse_iso_timestamp(["2026-09-07T10:20:30Z"]) is None
