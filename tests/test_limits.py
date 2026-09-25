import pytest

from jev_errors import JevBudgetError
from limits import (
    MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS,
    MAX_TOTAL_TOKENS,
    TRUNCATION_MARKER,
    estimate_tokens,
    fit_state,
    stringify_state,
    truncate_text,
)


class TestEstimateTokens:
    def test_ascii_quarter(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 10) == 3

    def test_non_ascii_counts_full(self):
        assert estimate_tokens("é") == 1

    def test_state_rendering(self):
        assert stringify_state("x") == "x"
        assert stringify_state({"a": 1}) == '{"a": 1}'
        assert stringify_state(None) == ""


class TestTruncateText:
    def test_short_text_unchanged(self):
        assert truncate_text("hello", 100) == "hello"

    def test_long_text_gets_marker(self):
        out = truncate_text("x" * 100, 20)
        assert out.endswith(TRUNCATION_MARKER)
        assert len(out) <= 20

    def test_never_splits_surrogate_pair(self):
        text = "a" * 10 + "\U0001F600" + "b" * 10
        out = truncate_text(text, 15)
        # Marker space reserved; must not end in a lone high surrogate.
        assert not (out and 0xD800 <= ord(out[-1]) <= 0xDBFF)


class TestFitState:
    def test_small_state_complete(self):
        result = fit_state("hello world", {"q": {"type": "noul", "instructions": "x"}})
        assert result["state"] == "hello world"
        assert result["truncated"] is False
        assert result["coverage"]["complete"] is True

    def test_huge_state_truncated(self):
        state = "a" * (MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS * 4 * 2)
        result = fit_state(state, {"q": {"type": "noul", "instructions": "x"}})
        assert result["truncated"] is True
        assert result["coverage"]["complete"] is False
        assert result["coverage"]["original_chars"] > result["coverage"]["evaluated_chars"]

    def test_state_stays_below_budget_after_fit(self):
        state = "z" * (MAX_TOTAL_TOKENS * 4 * 2)
        result = fit_state(state, {"q": {"type": "noul", "instructions": "x"}})
        assert result["truncated"] is True
        assert estimate_tokens(result["state"]) <= MAX_TOTAL_TOKENS

    def test_questions_over_budget_raise(self):
        with pytest.raises(JevBudgetError):
            fit_state("x", {"q": {"type": "noul", "instructions": "y" * 400_000}})

    def test_fit_state_non_dict_questions(self):
        result = fit_state("hello", None)
        assert result["state"] == "hello"
        assert result["coverage"]["estimated_tokens"]["questions"] == 0
        assert result["coverage"]["estimated_tokens"]["longest_question"] == 0


class TestCharBudgetForTokens:
    def test_zero_or_negative_budget(self):
        from limits import _char_budget_for_tokens
        assert _char_budget_for_tokens("hello", 0) == 0
        assert _char_budget_for_tokens("hello", -5) == 0

    def test_ascii_budget_calculation(self):
        from limits import _char_budget_for_tokens
        # marker is 4 tokens. For budget = 10, available = 6. 6 / 0.25 = 24 ascii chars.
        text = "a" * 100
        assert _char_budget_for_tokens(text, 10) == 24

    def test_non_ascii_budget_calculation(self):
        from limits import _char_budget_for_tokens
        # marker is 4 tokens. For budget = 10, available = 6. 6 / 1.0 = 6 non-ascii chars.
        text = "é" * 100
        assert _char_budget_for_tokens(text, 10) == 6


class TestTruncateToTokenBudget:
    def test_zero_or_negative_budget_returns_empty(self):
        from limits import truncate_to_token_budget
        assert truncate_to_token_budget("hello", 0) == ""
        assert truncate_to_token_budget("hello", -1) == ""

    def test_text_already_within_budget_returns_unchanged(self):
        from limits import truncate_to_token_budget
        assert truncate_to_token_budget("hello", 10) == "hello"

    def test_ascii_truncation_fits_budget(self):
        from limits import truncate_to_token_budget
        text = "a" * 500
        truncated = truncate_to_token_budget(text, 20)
        assert truncated.endswith(TRUNCATION_MARKER)
        assert estimate_tokens(truncated) <= 20

    def test_non_ascii_truncation_fits_budget(self):
        from limits import truncate_to_token_budget
        text = "é" * 100
        truncated = truncate_to_token_budget(text, 15)
        assert truncated.endswith(TRUNCATION_MARKER)
        assert estimate_tokens(truncated) <= 15

    def test_mixed_text_truncation_fits_budget(self):
        from limits import truncate_to_token_budget
        text = ("abc" + "é" + "def" + "🔥") * 50
        for b in [5, 10, 25, 50]:
            truncated = truncate_to_token_budget(text, b)
            assert estimate_tokens(truncated) <= b

    def test_budget_smaller_than_marker(self):
        from limits import truncate_to_token_budget
        # marker is 4 tokens. Test budgets 1, 2, 3
        text = "hello world this is a test"
        for b in [1, 2, 3]:
            truncated = truncate_to_token_budget(text, b)
            assert estimate_tokens(truncated) <= b

    def test_surrogate_pair_not_split(self):
        from limits import truncate_to_token_budget
        # Unicode emoji whose UTF-16 representation is a surrogate pair
        text = "a" * 23 + "\U0001F600" + "b" * 50
        truncated = truncate_to_token_budget(text, 10)
        assert estimate_tokens(truncated) <= 10
        # If truncated with marker, the prefix before marker shouldn't end in high surrogate
        prefix = truncated[:-len(TRUNCATION_MARKER)]
        assert not (prefix and 0xD800 <= ord(prefix[-1]) <= 0xDBFF)