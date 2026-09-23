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