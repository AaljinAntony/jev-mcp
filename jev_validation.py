"""Fail-closed response envelope validation.

Borrowed from `reference/burnigtm-jev-mcp/src/responses.ts` (MIT). Validates a
`system_one` response against the questions that produced it before any policy
function reads a number. Invalid responses raise `JevResponseError`; they are
never read as `safe:true`.
"""

from typesafe_sdk import Answer, Choice, Noul, Score

from jev_errors import JevResponseError
from policy import PROBABILITY_SUM_TOLERANCE, SCORE_MEAN_TOLERANCE

_ANSWER_TYPES = {"noul", "choice", "score"}


def _attr(obj, name):
    """Read an attribute off an SDK object or a raw dict answer."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int_key(key) -> int:
    if isinstance(key, bool):
        raise ValueError
    if isinstance(key, int):
        return key
    return int(key)


def _non_negative_number(value, what: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise JevResponseError(f"invalid {what}: expected a number")
    if value < 0:
        raise JevResponseError(f"invalid {what}: must be non-negative")


def _validate_noul(answer, name: str) -> None:
    value = _attr(answer, "noul")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise JevResponseError(f"answer '{name}' noul must be a number")
    if value < 0 or value > 1:
        raise JevResponseError(f"answer '{name}' noul must be in [0, 1]")


def _validate_probabilities(probabilities, keys_expected, name: str, kind: str) -> None:
    actual = list(probabilities.keys())
    if set(actual) != set(keys_expected):
        raise JevResponseError(f"answer '{name}' probabilities must cover exactly the {kind} criteria")
    if not probabilities:
        raise JevResponseError(f"answer '{name}' probabilities must not be empty")
    total = 0.0
    for option, prob in probabilities.items():
        _non_negative_number(prob, f"probability for '{option}'")
        if prob > 1:
            raise JevResponseError(f"answer '{name}' probability for '{option}' exceeds 1")
        total += prob
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise JevResponseError(f"answer '{name}' probabilities sum {total:.4f}, expected ~1")


def _validate_confidence(answer, name: str) -> float:
    confidence = _attr(answer, "confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise JevResponseError(f"answer '{name}' confidence must be a number")
    if confidence < 0 or confidence > 1:
        raise JevResponseError(f"answer '{name}' confidence must be in [0, 1]")
    return float(confidence)


def _validate_choice(answer, criteria, name: str) -> None:
    choice = _attr(answer, "choice")
    if not isinstance(choice, str) or choice not in criteria:
        raise JevResponseError(f"answer '{name}' choice must be one of the provided criteria")
    probabilities = _attr(answer, "probabilities")
    if not isinstance(probabilities, dict):
        raise JevResponseError(f"answer '{name}' probabilities must be an object")
    _validate_probabilities(probabilities, list(criteria.keys()), name, "choice")
    # The selected option must be the top probability — a selection that
    # contradicts its own distribution is not accepted.
    top = max(probabilities.values())
    if probabilities.get(choice, 0.0) + 1e-9 < top:
        raise JevResponseError(f"answer '{name}' selected choice '{choice}' is not the most probable option")
    _validate_confidence(answer, name)


def _validate_score(answer, criteria, name: str) -> None:
    n = len(criteria)
    if n <= 0:
        raise JevResponseError(f"question '{name}' has an empty score rubric")
    levels = [str(i) for i in range(n)]
    score = _attr(answer, "score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise JevResponseError(f"answer '{name}' score must be a number")
    if score < 0 or score > n - 1 + 1e-9:
        raise JevResponseError(f"answer '{name}' score must be within the rubric levels [0, {n - 1}]")
    probabilities = _attr(answer, "probabilities")
    if not isinstance(probabilities, dict):
        raise JevResponseError(f"answer '{name}' probabilities must be an object")
    try:
        normalized_keys = [_int_key(k) for k in probabilities.keys()]
    except (ValueError, TypeError):
        raise JevResponseError(f"answer '{name}' probabilities must be keyed by score level")
    expected_int = list(range(n))
    if sorted(normalized_keys) != expected_int:
        raise JevResponseError(f"answer '{name}' probabilities must cover exactly the score levels 0..{n - 1}")
    numeric_probs = {_int_key(k): float(probabilities[k]) for k in probabilities}
    _validate_probabilities(numeric_probs, expected_int, name, "score")
    legend = _attr(answer, "legend")
    if not isinstance(legend, dict):
        raise JevResponseError(f"answer '{name}' legend must be an object")
    try:
        legend_keys = [_int_key(k) for k in legend.keys()]
    except (ValueError, TypeError):
        raise JevResponseError(f"answer '{name}' legend must be keyed by score level")
    if sorted(legend_keys) != expected_int:
        raise JevResponseError(f"answer '{name}' legend must cover exactly the score levels 0..{n - 1}")
    expected_mean = sum(float(i) * p for i, p in numeric_probs.items())
    if abs(float(score) - expected_mean) > SCORE_MEAN_TOLERANCE:
        raise JevResponseError(
            f"answer '{name}' score {score:.4f} contradicts its distribution mean {expected_mean:.4f}"
        )
    _validate_confidence(answer, name)


def _validate_usage(raw) -> None:
    usage = _attr(raw, "usage")
    if not isinstance(usage, dict) and not hasattr(usage, "input_tokens"):
        raise JevResponseError("response missing usage")
    input_tokens = _attr(usage, "input_tokens")
    output_tokens = _attr(usage, "output_tokens")
    if input_tokens is not None:
        _non_negative_number(input_tokens, "usage.input_tokens")
    if output_tokens is not None:
        _non_negative_number(output_tokens, "usage.output_tokens")


def validate_response(raw, questions):
    """Validate a `system_one` response against its questions.

    Fails closed with `JevResponseError` when:
      - answers keys don't match question keys exactly,
      - an answer's type differs from its question,
      - noul/choice/score values are malformed or self-contradictory,
      - `model` or `usage` are missing.

    Returns the (already parsed) response on success.
    """
    answers = _attr(raw, "answers")
    if not isinstance(answers, dict):
        raise JevResponseError("response missing 'answers' object")
    if not questions or not isinstance(questions, dict):
        raise JevResponseError("questions must be a non-empty mapping")
    if set(answers.keys()) != set(questions.keys()):
        raise JevResponseError("response answers must match the questions exactly")
    model = _attr(raw, "model")
    if not isinstance(model, str) or not model:
        raise JevResponseError("response missing 'model'")
    _validate_usage(raw)

    for name, question in questions.items():
        answer = answers.get(name)
        if answer is None:
            raise JevResponseError(f"missing answer for '{name}'")
        answer_type = _attr(answer, "type")
        question_type = _attr(question, "type")
        if answer_type not in _ANSWER_TYPES:
            raise JevResponseError(f"answer '{name}' has unknown type {answer_type!r}")
        if question_type != answer_type:
            raise JevResponseError(f"answer '{name}' type {answer_type!r} does not match question type {question_type!r}")
        if answer_type == "noul":
            _validate_noul(answer, name)
        elif answer_type == "choice":
            criteria = _attr(question, "criteria")
            if not isinstance(criteria, dict):
                raise JevResponseError(f"question '{name}' criteria must be a mapping of options")
            _validate_choice(answer, criteria, name)
        elif answer_type == "score":
            criteria = _attr(question, "criteria")
            if not isinstance(criteria, list):
                raise JevResponseError(f"question '{name}' criteria must be a rubric list")
            _validate_score(answer, criteria, name)
    return raw