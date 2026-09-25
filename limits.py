"""Token budget estimation and input fitting.

Borrowed from `reference/burnigtm-jev-mcp/src/limits.ts` (MIT). Keeps state
inside the estimated TypeSafe context budget and reports truncation so policy
never auto-accepts on partial context.
"""

import json

from jev_errors import JevBudgetError

#: Estimated total TypeSafe Jev context: all state + all questions.
MAX_TOTAL_TOKENS = 64_000
#: TypeSafe Jev context: state + the longest question.
MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS = 32_000
#: Choice option cap (TypeSafe supports up to 255; community servers use 250).
MAX_CHOICE_OPTIONS = 250
#: Per-content read cap for resources.
MAX_CONTENT_CHARS = 6_000

TRUNCATION_MARKER = "\n…[truncated]"


def estimate_tokens(value) -> int:
    """Rough token estimate: ASCII chars / 4 + non-ASCII chars."""
    text = stringify_state(value)
    ascii_chars = 0
    other_chars = 0
    for char in text:
        if ord(char) <= 0x7F:
            ascii_chars += 1
        else:
            other_chars += 1
    return __import__("math").ceil(ascii_chars / 4 + other_chars)


def stringify_state(state) -> str:
    """Render state for the token estimate."""
    if isinstance(state, str):
        return state
    if state is None:
        return ""
    return json.dumps(state, default=str)


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate with an explicit marker, never splitting a surrogate pair."""
    if len(text) <= max_chars:
        return text
    limit = max(0, int(max_chars))
    marker = TRUNCATION_MARKER
    if limit < len(marker):
        return marker[:limit]
    end = limit - len(marker)
    if end > 0 and len(text) > end - 1 and 0xD800 <= ord(text[end - 1]) <= 0xDBFF:
        end -= 1
    return text[:end] + marker


def _stringify_json(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, default=str)


def longest_question_tokens(questions) -> int:
    """Largest estimated token count across all questions."""
    if not questions or not hasattr(questions, "items"):
        return 0
    longest = 0
    for value in questions.values():
        longest = max(longest, estimate_tokens(value))
    return longest


def fit_state(state, questions) -> dict:
    """Fit ``state`` inside the estimated budget.

    Returns ``{state, truncated, coverage}`` where ``coverage`` mirrors the
    burnigtm shape: ``{complete, original_chars, evaluated_chars,
    estimated_tokens, estimator}``. Over-budget state is truncated; questions
    over budget raise ``JevBudgetError``.
    """
    question_token_counts = {}
    if questions and hasattr(questions, "items"):
        for key, value in questions.items():
            question_token_counts[key] = estimate_tokens(value)

    questions_tokens = sum(question_token_counts.values()) if question_token_counts else estimate_tokens(questions)
    longest_tokens = max(question_token_counts.values()) if question_token_counts else 0

    budget = min(
        MAX_TOTAL_TOKENS - questions_tokens,
        MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS - longest_tokens,
    )
    if budget < 0:
        raise JevBudgetError(
            "Questions exceed the estimated context budget. Shorten question instructions or criteria, or split the request."
        )
    raw = stringify_state(state)
    tokens = estimate_tokens(raw)
    truncated = tokens > budget
    fitted = truncate_to_token_budget(raw, budget) if truncated else raw
    evaluated = fitted
    if truncated and len(fitted) >= len(TRUNCATION_MARKER):
        evaluated = fitted[: -len(TRUNCATION_MARKER)]
    return {
        "state": fitted if truncated else state,
        "truncated": truncated,
        "coverage": {
            "complete": not truncated,
            "original_chars": len(raw),
            "evaluated_chars": len(evaluated),
            "estimated_tokens": {
                "state": estimate_tokens(fitted),
                "questions": questions_tokens,
                "longest_question": longest_tokens,
            },
            "estimator": "chars/4",
        },
    }


def _char_budget_for_tokens(text: str, token_budget: int) -> int:
    """Compute the maximum character count that fits within a token budget.

    Since estimate_tokens uses ceil(ascii/4 + non_ascii), the worst case
    is all non-ASCII (1 token per char) and the best case is all ASCII
    (4 chars per token). We scan to find the exact cutoff.
    """
    if token_budget <= 0:
        return 0
    marker_tokens = estimate_tokens(TRUNCATION_MARKER)
    available = token_budget - marker_tokens
    if available <= 0:
        return 0

    tokens_used = 0.0
    for i, char in enumerate(text):
        cost = 0.25 if ord(char) <= 0x7F else 1.0
        if tokens_used + cost > available:
            return i
        tokens_used += cost
    return len(text)


def truncate_to_token_budget(text: str, budget: int) -> str:
    """Truncate text to fit within the estimated token budget."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if len(text) <= budget * 4 and estimate_tokens(text) <= budget:
        return text

    marker_tokens = estimate_tokens(TRUNCATION_MARKER)
    if budget < marker_tokens:
        candidate = truncate_text(text, budget * 4)
        while estimate_tokens(candidate) > budget and len(candidate) > 0:
            candidate = candidate[:-1]
        return candidate

    max_chars = _char_budget_for_tokens(text, budget)
    end = max_chars
    if end > 0 and len(text) > end - 1 and 0xD800 <= ord(text[end - 1]) <= 0xDBFF:
        end -= 1
    return text[:end] + TRUNCATION_MARKER