import json
import math

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


class TestEstimateTokensExactness:
    """The regex fast path must be bit-identical to the original Python loop.

    Spelled out longhand on purpose: the test must not import the function it is
    verifying.
    """

    @staticmethod
    def _reference(value) -> int:
        text = value if isinstance(value, str) else ("" if value is None else json.dumps(value, default=str))
        ascii_chars = 0
        other_chars = 0
        for char in text:
            if ord(char) <= 0x7F:
                ascii_chars += 1
            else:
                other_chars += 1
        return math.ceil(ascii_chars / 4 + other_chars)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "abc",
            "a" * 4000,
            "中文字" * 500,
            "mix é 123",
            "emoji 🎉 test",
            "\x00\x7f\x80",
            "\ud83d",                       # lone surrogate
            "\udc00",                       # lone low surrogate
            "a\ud83d\ude00b",               # paired surrogate inside ASCII
            {"k": "é" * 100, "n": [1, 2, 3]},
            ["🎉", "x", "y"],
        ],
    )
    def test_matches_reference_implementation(self, value):
        assert estimate_tokens(value) == self._reference(value)

    def test_empty_and_none(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0

    def test_fast_path_boundary_lengths(self):
        # `len(text) <= budget * 4` is the early-out in truncate_to_token_budget;
        # pin the token count either side of it.
        for length in (3999, 4000, 4001):
            assert estimate_tokens("a" * length) == self._reference("a" * length)


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

    def test_never_exceeds_budget_over_random_inputs(self):
        import random

        from limits import truncate_to_token_budget
        rng = random.Random(20260926)
        alphabets = ["abc ", "é中", "🎉🚀", "a\ud83d\ude00b", "\x00\x7f", "lorem ipsum dolor "]
        for _ in range(200):
            alphabet = rng.choice(alphabets)
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 300)))
            budget = rng.randint(1, 80)
            truncated = truncate_to_token_budget(text, budget)
            assert estimate_tokens(truncated) <= budget, (text[:40], budget)

    def test_binary_search_never_splits_a_surrogate_at_the_boundary(self):
        from limits import truncate_to_token_budget
        # Every budget whose cut point lands on a surrogate pair boundary.
        for budget in range(4, 40):
            text = "a" * 60 + "\U0001F600" * 20
            truncated = truncate_to_token_budget(text, budget)
            assert estimate_tokens(truncated) <= budget
            body = truncated[:-len(TRUNCATION_MARKER)] if truncated.endswith(TRUNCATION_MARKER) else truncated
            assert not (body and 0xD800 <= ord(body[-1]) <= 0xDBFF), budget


class TestLinearScanRemoved:
    def test_char_budget_for_tokens_is_gone(self):
        import limits
        # The binary search in `truncate_to_token_budget` replaced the linear
        # scan; a stale caller must fail loudly rather than silently re-add it.
        assert not hasattr(limits, "_char_budget_for_tokens")