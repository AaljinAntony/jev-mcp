"""Deterministic offline Jev judge for `JEV_MCP_MOCK=1`.

Borrowed from `reference/burnigtm-jev-mcp/src/mock.ts` (MIT). Returns
plausible, reproducible answers using keyword/overlap heuristics so the server
runs and the MCP tool tests pass without a `TYPESAFE_API_KEY`. Documentation
and tests/demos only — never a substitute for a real TypeSafe decision.
"""

import math
import re
from functools import lru_cache

from typesafe_sdk import (
    ChoiceAnswer,
    NoulAnswer,
    ScoreAnswer,
    SystemOneResponse,
    Usage,
)

from limits import estimate_tokens, stringify_state
from policy import confidence_from_probabilities
from jev_validation import _assert_finite_json


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return stringify_state(value)


def _tokenize(text: str):
    return re.split(r"[^a-z0-9]+", text.lower())


#: Words that carry no routing signal. They appear in every goal sentence and in
#: most candidate previews, so counting them would let any option score a hit.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "not", "but",
    "you", "your", "are", "was", "were", "has", "have", "had", "its", "it's",
    "which", "what", "when", "where", "does", "must", "should", "can", "will",
    "any", "all", "each", "per", "via", "use", "using", "used", "task", "goal",
    "file", "files", "select", "single", "supplied", "candidate", "candidates",
    "none", "true", "false", "yes", "answer", "answers", "question", "state",
    "a", "an", "of", "to", "in", "is", "it", "be", "or", "on", "at", "by", "if",
    "describe", "described", "describes", "following", "such", "than", "then",
}


def _token_set(text: str) -> frozenset:
    """The state tokens, computed **once** per request.

    Every judge in this module used to re-tokenize the whole state for each
    option of each question — hundreds of full passes over a 100k-char state
    per call. One set is derived here and threaded down instead.
    """
    return frozenset(_tokenize(text))


def _overlap_set(left, right_text: str) -> float:
    """Fraction of `right_text`'s meaningful tokens present in `left`."""
    right = [t for t in _tokenize(right_text) if len(t) > 1]
    if not right:
        return 0.0
    hits = sum(1 for token in right if token in left)
    return hits / len(right)


def _overlap(a: str, b: str) -> float:
    """Overlap between two texts. Kept for callers that have no shared set."""
    return _overlap_set(_token_set(a), b)


def _state_terms(text: str) -> set:
    """The meaningful tokens of the state, once per request."""
    return _terms_from(_tokenize(text))


@lru_cache(maxsize=4096)
def _description_tokens(description: str) -> frozenset:
    """Tokens of one candidate description, memoized.

    The same 250 descriptions arrive in every Choice of a request and again on
    the next call for the same workspace, so this is the difference between
    tokenizing 250 strings and tokenizing 1,500 of them. Pure function of the
    string, so the cache cannot change a judgment; the bound keeps it from
    growing with the workspace.
    """
    return frozenset(_tokenize(description))


def _terms_from(tokens) -> set:
    return {t for t in tokens if len(t) > 1 and t not in _STOPWORDS}


def _resolve_terms(state_text: str, state_tokens, terms):
    """The precomputed terms if the caller has them, else derive them once."""
    if terms is not None:
        return terms
    if state_tokens is not None:
        return _terms_from(state_tokens)
    return _state_terms(state_text)


def _document_frequency(texts) -> dict:
    """How many candidates contain each token."""
    frequency: dict = {}
    for text in texts:
        for token in _description_tokens(text):
            frequency[token] = frequency.get(token, 0) + 1
    return frequency


def _evidence_score(terms: set, description: str, frequency: dict) -> float:
    """Inverse-frequency weight of the state terms one candidate contains.

    Counting shared words is not enough with 250 candidates: a word that appears
    in two files means much more than a word that appears in fifty. Weighting by
    `1 / candidates containing the term` makes a rare, task-specific word dominate
    a word every option happens to share.
    """
    if not terms:
        return 0.0
    tokens = _description_tokens(description)
    return sum(1.0 / frequency.get(token, 1) for token in terms if token in tokens)


def _clamp01(value: float) -> float:
    return min(0.99, max(0.01, value))


def _softmax(scores) -> list:
    max_score = max(scores) if scores else 0
    exps = [math.exp(s - max_score) for s in scores]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


def _mock_noul(
    state_text: str,
    instructions: str,
    presence: bool = False,
    options=None,
    frequency=None,
    state_tokens=None,
    terms=None,
) -> float:
    if presence:
        # A presence Noul (one that carries true/false criteria) asks whether any
        # candidate actually fits. Answer it from the candidate evidence itself:
        # that is the only thing the real model judges against too. It is checked
        # *before* the command regexes on purpose — `select_target_files` asks
        # exactly this question, and a task that happens to contain "delete" must
        # not turn the presence answer into a confident 0.97.
        terms = _resolve_terms(state_text, state_tokens, terms)
        frequency = frequency or _document_frequency(options or [])
        best = max((_evidence_score(terms, text, frequency) for text in (options or [])), default=0.0)
        if best < EVIDENCE_MIN_SCORE:
            return 0.1
        if best < 1.0:
            return 0.6
        return _clamp01(0.6 + 0.1 * best)
    hay = state_text.lower()
    if re.search(
        r"delete|drop table|wipe|truncate|rm -rf|force push|--force|reset --hard|filter-branch|git config --|clean -fdx",
        hay,
    ):
        return 0.97
    if re.search(r"git status|git add|git diff|git stash|ls\b|cat |echo |mkdir|touch |pip install", hay):
        return 0.03
    left = _token_set(state_text) if state_tokens is None else state_tokens
    return _clamp01(0.35 + 0.5 * _overlap_set(left, instructions))


def _question_attr(question, name):
    if isinstance(question, dict):
        return question.get(name)
    return getattr(question, name, None)


def _candidate_evidence(questions) -> list:
    """Every real Choice option's description in the request.

    The `none` escape hatch is excluded: it restates the goal in the model's own
    words, so it would look like the strongest evidence in the request.
    """
    texts = []
    for question in questions.values():
        if _question_attr(question, "type") != "choice":
            continue
        criteria = _question_attr(question, "criteria")
        items = criteria.items() if hasattr(criteria, "items") else []
        texts.extend(_as_text(value) for label, value in items if label != "none" and value is not None)
    return texts


_TIER_BOOSTS = {
    "frontier": (["architecture", "refactor across", "design", "complex", "multi-file", "race", "deadlock"], 3.0),
    "balanced": (["bug", "test", "feature", "isolated"], 1.5),
    "fast": (["typo", "lookup", "docstring", "rename", "format"], 1.5),
}

#: Evidence weight a candidate needs before the mock treats it as a real match.
#: One word shared with a hundred files is not evidence.
EVIDENCE_MIN_SCORE = 0.5


def _score_option(terms: set, description: str, frequency: dict, haystack: str, label: str) -> float:
    score = 3.0 * _evidence_score(terms, description, frequency)
    for key, (patterns, amount) in _TIER_BOOSTS.items():
        if label == key and any(p in haystack for p in patterns):
            score += amount
    return score


def _mock_choice(state_text: str, question, frequency: dict = None, state_tokens=None, terms=None) -> ChoiceAnswer:
    labels = list(question.criteria.keys())
    descriptions = [_as_text(question.criteria.get(label)) for label in labels]
    if frequency is None:
        frequency = _document_frequency([d for l, d in zip(labels, descriptions) if l != "none"])
    terms = _resolve_terms(state_text, state_tokens, terms)
    haystack = state_text.lower()

    # `none` is the escape hatch, not a competitor. It scores zero and only wins
    # when no supplied candidate carries enough task-specific evidence to believe.
    scores = [
        0.0 if label == "none" else _score_option(terms, text, frequency, haystack, label)
        for label, text in zip(labels, descriptions)
    ]
    if "none" in labels:
        candidates = [s for label, s in zip(labels, scores) if label != "none"]
        if not candidates or max(candidates) < EVIDENCE_MIN_SCORE:
            scores[labels.index("none")] = max(scores) + 1.0

    probabilities = dict(zip(labels, _softmax(scores)))
    choice = labels[max(range(len(labels)), key=lambda i: probabilities[labels[i]])]
    return ChoiceAnswer(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence_from_probabilities(probabilities),
    )


def _mock_score(state_text: str, question, state_tokens=None) -> ScoreAnswer:
    instructions = _as_text(question.instructions)
    levels = [_as_text(c) for c in question.criteria]
    left = _token_set(state_text) if state_tokens is None else state_tokens
    scores = []
    for index, level in enumerate(levels):
        scores.append(_overlap_set(left, f"{instructions} {level}") + index * 0.05)
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


def mock_system_one(state, questions, model="jev-latest", input_tokens=None):
    """Deterministic `system_one` judge for the mock mode.

    Returns a real `SystemOneResponse` (SDK objects) so the validation layer
    runs against the exact shapes production uses.

    `input_tokens` may be supplied by the caller from `fit_state`'s coverage,
    which already counted the state and every question value. Recomputing them
    here re-serialized and re-scanned the whole payload for a number the caller
    is holding; it is only estimated here when nobody supplied it.
    """
    state_text = stringify_state(state)
    # One tokenization for the whole request, threaded into every question.
    state_tokens = _token_set(state_text)
    terms = _terms_from(state_tokens)
    options = _candidate_evidence(questions)
    frequency = _document_frequency(options)
    answers = {}
    for name, question in questions.items():
        qtype = _question_attr(question, "type")
        if qtype == "noul":
            instructions = _as_text(_question_attr(question, "instructions"))
            presence = _question_attr(question, "criteria") is not None
            answers[name] = NoulAnswer(
                noul=round(
                    _mock_noul(
                        state_text, instructions, presence, options, frequency,
                        state_tokens=state_tokens, terms=terms,
                    ),
                    2,
                )
            )
        elif qtype == "choice":
            answers[name] = _mock_choice(
                state_text, question, frequency, state_tokens=state_tokens, terms=terms
            )
        elif qtype == "score":
            answers[name] = _mock_score(state_text, question, state_tokens=state_tokens)
        else:
            raise ValueError(f"mock cannot answer question type {qtype!r}")

    if input_tokens is None:
        input_tokens = estimate_tokens(state) + estimate_tokens(questions)

    res = SystemOneResponse(
        model=f"{model}+mock",
        answers=answers,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=len(questions) * 8,
        ),
    )
    _assert_finite_json(res)
    return res
