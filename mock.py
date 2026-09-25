"""Deterministic offline Jev judge for `JEV_MCP_MOCK=1`.

Borrowed from `reference/burnigtm-jev-mcp/src/mock.ts` (MIT). Returns
plausible, reproducible answers using keyword/overlap heuristics so the server
runs and the MCP tool tests pass without a `TYPESAFE_API_KEY`. Documentation
and tests/demos only — never a substitute for a real TypeSafe decision.
"""

import math
import re

from typesafe_sdk import (
    ChoiceAnswer,
    NoulAnswer,
    ScoreAnswer,
    SystemOneResponse,
    Usage,
)

from limits import estimate_tokens, stringify_state
from policy import confidence_from_probabilities


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return stringify_state(value)


def _tokenize(text: str):
    return re.split(r"[^a-z0-9]+", text.lower())


def _overlap(a: str, b: str) -> float:
    left = set(_tokenize(a))
    right = [t for t in _tokenize(b) if len(t) > 1]
    if not right:
        return 0.0
    hits = sum(1 for token in right if token in left)
    return hits / len(right)


def _clamp01(value: float) -> float:
    return min(0.99, max(0.01, value))


def _softmax(scores) -> list:
    max_score = max(scores) if scores else 0
    exps = [math.exp(s - max_score) for s in scores]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


def _mock_noul(state_text: str, instructions: str) -> float:
    hay = state_text.lower()
    if re.search(
        r"delete|drop table|wipe|truncate|rm -rf|force push|--force|reset --hard|filter-branch|git config --|clean -fdx",
        hay,
    ):
        return 0.97
    if re.search(r"git status|git add|git diff|git stash|ls\b|cat |echo |mkdir|touch |pip install", hay):
        return 0.03
    return _clamp01(0.35 + 0.5 * _overlap(state_text, instructions))


def _score_option(state_text: str, label: str, description: str, instructions: str) -> float:
    score = _overlap(state_text, f"{label} {description}") * 3 + _overlap(state_text, instructions)
    haystack = state_text.lower()
    boosts = {
        "none": (["none of the", "no relevant", "does not", "not related"], 4.0),
        "frontier": (["architecture", "refactor across", "design", "complex", "multi-file", "race", "deadlock"], 3.0),
        "balanced": (["bug", "test", "feature", "isolated"], 1.5),
        "fast": (["typo", "lookup", "docstring", "rename", "format"], 1.5),
    }
    for key, (patterns, amount) in boosts.items():
        if label == key and any(p in haystack for p in patterns):
            score += amount
    if label == "primary" or label == "target_file":
        score += _overlap(state_text, label) * 1.5
    if label == "secondary" or label == "tertiary":
        score += _overlap(state_text, label) * 0.5
    return score


def _mock_choice(state_text: str, question) -> ChoiceAnswer:
    instructions = _as_text(question.instructions)
    labels = list(question.criteria.keys())
    scores = [
        _score_option(state_text, label, _as_text(question.criteria.get(label)), instructions)
        for label in labels
    ]
    probabilities = dict(zip(labels, _softmax(scores)))
    choice = labels[int(max(range(len(labels)), key=lambda i: scores[i]))]
    return ChoiceAnswer(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence_from_probabilities(probabilities),
    )


def _mock_score(state_text: str, question) -> ScoreAnswer:
    instructions = _as_text(question.instructions)
    levels = [_as_text(c) for c in question.criteria]
    scores = []
    for index, level in enumerate(levels):
        scores.append(_overlap(state_text, f"{instructions} {level}") + index * 0.05)
    probabilities = dict(
        (str(i), p) for i, p in enumerate(_softmax(scores))
    )
    legend = {str(i): level for i, level in enumerate(levels)}
    score = sum(float(i) * p for i, p in probabilities.items())
    return ScoreAnswer(
        score=score,
        probabilities={
            int(k): p
            for k, p in probabilities.items()
        },
        legend={int(k): v for k, v in legend.items()},
        confidence=confidence_from_probabilities(probabilities),
    )


def mock_system_one(state, questions, model="jev-latest"):
    """Deterministic `system_one` judge for the mock mode.

    Returns a real `SystemOneResponse` (SDK objects) so the validation layer
    runs against the exact shapes production uses.
    """
    state_text = stringify_state(state)
    answers = {}
    for name, question in questions.items():
        qtype = question.type if not isinstance(question, dict) else question.get("type")
        if qtype == "noul":
            instructions = _as_text(question.instructions)
            answers[name] = NoulAnswer(noul=round(_mock_noul(state_text, instructions), 2))
        elif qtype == "choice":
            answers[name] = _mock_choice(state_text, question)
        elif qtype == "score":
            answers[name] = _mock_score(state_text, question)
        else:
            raise ValueError(f"mock cannot answer question type {qtype!r}")

    return SystemOneResponse(
        model=f"{model}+mock",
        answers=answers,
        usage=Usage(
            input_tokens=estimate_tokens({"state": state, "questions": questions}),
            output_tokens=len(questions) * 8,
        ),
    )