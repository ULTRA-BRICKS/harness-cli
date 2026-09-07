from __future__ import annotations

from harness_cli.model import UsageTotals, classify_error_text, extract_reset_hint

# The strings below are the REAL isApiErrorMessage texts observed on this machine's own
# transcripts (~136 events across ~/.claude and ~/.claude-accounts/account-*, 2026-08/09),
# not guessed placeholders.


def test_classify_error_text_session_limit():
    text = "You've hit your session limit · resets 7:30pm (Europe/Belgrade)"
    assert classify_error_text(text) == "session_limit"


def test_classify_error_text_weekly_limit():
    text = "You've hit your weekly limit · resets 8pm (Europe/Belgrade)"
    assert classify_error_text(text) == "weekly_limit"


def test_classify_error_text_model_limit():
    text = "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model."
    assert classify_error_text(text) == "model_limit"


def test_classify_error_text_server_overload_is_not_your_usage_limit():
    text = "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
    assert classify_error_text(text) == "rate_limit"


def test_classify_error_text_safety_flag():
    text = "API Error: Fable 5's safeguards flagged this message (https://www.anthropic.com/legal/aup)."
    assert classify_error_text(text) == "safety_flag"


def test_extract_reset_hint_present():
    text = "You've hit your session limit · resets 7:30pm (Europe/Belgrade)"
    assert extract_reset_hint(text) == "7:30pm (Europe/Belgrade)"


def test_extract_reset_hint_absent():
    assert extract_reset_hint("Not logged in · Please run /login") is None


def test_classify_error_text_rate_limit():
    assert classify_error_text("Error: 429 rate limit exceeded") == "rate_limit"


def test_classify_error_text_overloaded():
    assert classify_error_text("Overloaded (529): please retry") == "overloaded"


def test_classify_error_text_auth():
    assert classify_error_text("Not logged in - Please run /login") == "auth"


def test_classify_error_text_unknown_is_other_not_fabricated():
    assert classify_error_text("Something entirely unrelated happened") == "other"


def test_usage_totals_add_sums_real_fields_only():
    totals = UsageTotals()
    totals.add(
        {
            "input_tokens": 2,
            "output_tokens": 10,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 100,
        }
    )
    totals.add({"input_tokens": 3, "output_tokens": 1})
    assert totals.input_tokens == 5
    assert totals.output_tokens == 11
    assert totals.cache_creation_input_tokens == 5
    assert totals.cache_read_input_tokens == 100
    assert totals.assistant_turns == 2
    assert totals.total_tokens == 5 + 11 + 5 + 100


def test_usage_totals_handles_missing_fields_as_zero_not_fabricated():
    totals = UsageTotals()
    totals.add({})
    assert totals.total_tokens == 0
    assert totals.assistant_turns == 1


def test_usage_totals_rejects_non_numeric_string_without_crashing():
    totals = UsageTotals()
    totals.add({"output_tokens": "unknown"})
    assert totals.output_tokens == 0


def test_usage_totals_rejects_bool_as_fabricated_token_count():
    # int(True) == 1 in Python - must not let a stray boolean report a real token.
    totals = UsageTotals()
    totals.add({"output_tokens": True})
    assert totals.output_tokens == 0


def test_usage_totals_rejects_list_value_without_crashing():
    totals = UsageTotals()
    totals.add({"output_tokens": [1, 2, 3]})
    assert totals.output_tokens == 0


def test_usage_totals_rejects_nan_without_crashing():
    totals = UsageTotals()
    totals.add({"output_tokens": float("nan")})
    assert totals.output_tokens == 0


def test_usage_totals_rejects_infinity_without_crashing():
    totals = UsageTotals()
    totals.add({"output_tokens": float("inf")})
    assert totals.output_tokens == 0


def test_usage_totals_rejects_negative_value():
    totals = UsageTotals()
    totals.add({"output_tokens": -5})
    assert totals.output_tokens == 0


def test_usage_totals_rejects_fractional_value():
    totals = UsageTotals()
    totals.add({"output_tokens": 2.7})
    assert totals.output_tokens == 0


def test_usage_totals_accepts_whole_float():
    totals = UsageTotals()
    totals.add({"output_tokens": 5.0})
    assert totals.output_tokens == 5


def test_usage_totals_rejects_absurdly_large_int_without_crashing():
    # A per-field value can be individually "valid" (a real Python int, within JSON's
    # own digit-conversion limit) yet still be nonsensical for a token count, and summing
    # two such huge values across fields could later overflow str-conversion at print
    # time even though nothing crashed at parse time. Reject before that can happen.
    totals = UsageTotals()
    totals.add({"input_tokens": 10**13, "output_tokens": 10**13})
    assert totals.input_tokens == 0
    assert totals.output_tokens == 0
    # A sane value must still work normally.
    totals2 = UsageTotals()
    totals2.add({"output_tokens": 10**9})
    assert totals2.output_tokens == 10**9
