import pytest

from typesafe_sdk import Choice, Noul, Score, SystemOneResponse, Usage

from jev_errors import JevResponseError
from jev_validation import validate_response


def _response(answers, model="jev-latest", usage=None):
    if usage is None:
        usage = Usage(input_tokens=10, output_tokens=6)
    try:
        return SystemOneResponse(model=model, answers=answers, usage=usage)
    except Exception:
        return {"model": model, "answers": answers, "usage": usage}


def _noul_questions():
    return {
        "is_destructive": Noul(instructions="Is it destructive?"),
        "modifies_git": Noul(instructions="Does it touch git?"),
    }


def _noul_answers(v1=0.03, v2=0.02):
    return {
        "is_destructive": {"type": "noul", "noul": v1},
        "modifies_git": {"type": "noul", "noul": v2},
    }


def _choice_questions():
    return {
        "primary": Choice(criteria={"a.md": "A skill", "b.md": "B skill"}, instructions="Pick one."),
    }


class TestStructure:
    def test_valid_noul_response(self):
        response = _response(_noul_answers())
        assert validate_response(response, _noul_questions()) is response

    def test_valid_choice_response(self):
        questions = _choice_questions()
        answers = {
            "primary": {
                "type": "choice",
                "choice": "a.md",
                "confidence": 0.9,
                "probabilities": {"a.md": 0.9, "b.md": 0.1},
            }
        }
        assert validate_response(_response(answers), questions) is not None

    def test_valid_score_response(self):
        questions = {
            "q": Score(criteria=["low", "mid", "high"], instructions="Rate it."),
        }
        answers = {
            "q": {
                "type": "score",
                "score": 1.0,
                "confidence": 0.8,
                "probabilities": {0: 0.1, 1: 0.8, 2: 0.1},
                "legend": {0: "low", 1: "mid", 2: "high"},
            }
        }
        assert validate_response(_response(answers), questions) is not None

    def test_score_with_string_keys_accepted(self):
        """Score probabilities with string keys (raw JSON) should pass validation."""
        questions = {"q": Score(criteria=["low", "mid", "high"], instructions="Rate")}
        answers = {
            "q": {
                "type": "score",
                "score": 1.0,
                "confidence": 0.8,
                "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1},  # string keys
                "legend": {"0": "low", "1": "mid", "2": "high"},     # string keys
            }
        }
        assert validate_response(_response(answers), questions) is not None

    def test_accepts_sdk_answer_objects(self):
        questions = _choice_questions()
        from typesafe_sdk import ChoiceAnswer

        answers = {
            "primary": ChoiceAnswer(
                choice="b.md",
                confidence=0.85,
                probabilities={"a.md": 0.15, "b.md": 0.85},
            )
        }
        assert validate_response(_response(answers), questions) is not None


class TestFailures:
    def test_extra_answer_key_rejected(self):
        questions = _choice_questions()
        answers = {
            "primary": {
                "type": "choice",
                "choice": "a.md",
                "confidence": 0.9,
                "probabilities": {"a.md": 0.9, "b.md": 0.1},
            },
            "unexpected": {"type": "noul", "noul": 0.5},
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), questions)

    def test_missing_answer_key_rejected(self):
        with pytest.raises(JevResponseError):
            validate_response(_response({"is_destructive": {"type": "noul", "noul": 0.1}}), _noul_questions())

    def test_wrong_answer_type_rejected(self):
        answers = {
            "is_destructive": {"type": "choice", "choice": "x", "confidence": 0.9, "probabilities": {"x": 1.0}},
            "modifies_git": {"type": "noul", "noul": 0.02},
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), _noul_questions())

    def test_noul_out_of_range_rejected(self):
        with pytest.raises(JevResponseError):
            validate_response(_response(_noul_answers(v1=1.5)), _noul_questions())
        with pytest.raises(JevResponseError):
            validate_response(_response(_noul_answers(v1=-0.1)), _noul_questions())

    def test_choice_not_in_criteria_rejected(self):
        answers = {
            "primary": {
                "type": "choice",
                "choice": "missing.md",
                "confidence": 0.9,
                "probabilities": {"a.md": 0.9, "b.md": 0.1},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), _choice_questions())

    def test_probability_sum_off_rejected(self):
        questions = _choice_questions()
        answers = {
            "primary": {
                "type": "choice",
                "choice": "a.md",
                "confidence": 0.9,
                "probabilities": {"a.md": 0.3, "b.md": 0.3},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), questions)

    def test_choice_contradicts_own_distribution_rejected(self):
        questions = _choice_questions()
        answers = {
            "primary": {
                "type": "choice",
                "choice": "b.md",
                "confidence": 0.9,
                "probabilities": {"a.md": 0.9, "b.md": 0.1},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), questions)

    def test_score_mean_mismatch_rejected(self):
        questions = {"q": Score(criteria=["low", "mid", "high"], instructions="Rate")}
        answers = {
            "q": {
                "type": "score",
                "score": 2.0,
                "confidence": 0.8,
                "probabilities": {0: 0.9, 1: 0.05, 2: 0.05},
                "legend": {0: "low", 1: "mid", 2: "high"},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), questions)

    def test_score_3_level_tolerance_boundary(self):
        # 3-level rubric: tolerance is 0.01 * (3 - 1) = 0.02
        questions = {"q": Score(criteria=["low", "mid", "high"], instructions="Rate")}
        # expected_mean = 0*0.1 + 1*0.8 + 2*0.1 = 1.0
        # 0.019 drift is within 0.02 tolerance
        ans_ok = {
            "q": {
                "type": "score",
                "score": 1.019,
                "confidence": 0.8,
                "probabilities": {0: 0.1, 1: 0.8, 2: 0.1},
                "legend": {0: "low", 1: "mid", 2: "high"},
            }
        }
        assert validate_response(_response(ans_ok), questions) is not None

        # 0.021 drift exceeds 0.02 tolerance
        ans_bad = {
            "q": {
                "type": "score",
                "score": 1.021,
                "confidence": 0.8,
                "probabilities": {0: 0.1, 1: 0.8, 2: 0.1},
                "legend": {0: "low", 1: "mid", 2: "high"},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(ans_bad), questions)

    def test_score_7_level_tolerance_accepts_larger_drift(self):
        # 7-level rubric: tolerance is 0.01 * (7 - 1) = 0.06
        levels = [f"level_{i}" for i in range(7)]
        questions = {"q": Score(criteria=levels, instructions="Rate")}
        # expected_mean is 3.0
        probs = {i: (1.0 if i == 3 else 0.0) for i in range(7)}
        legend = {i: levels[i] for i in range(7)}
        # Drift 0.05 is accepted (0.05 <= 0.06)
        ans_ok = {
            "q": {
                "type": "score",
                "score": 3.05,
                "confidence": 0.9,
                "probabilities": probs,
                "legend": legend,
            }
        }
        assert validate_response(_response(ans_ok), questions) is not None

    def test_score_2_level_tolerance_rejects_tight_drift(self):
        # 2-level rubric: tolerance is 0.01 * (2 - 1) = 0.01
        questions = {"q": Score(criteria=["no", "yes"], instructions="Rate")}
        # expected_mean = 0.5
        # Drift of 0.019 exceeds 0.01 tolerance
        ans = {
            "q": {
                "type": "score",
                "score": 0.519,
                "confidence": 0.5,
                "probabilities": {0: 0.5, 1: 0.5},
                "legend": {0: "no", 1: "yes"},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(ans), questions)

    def test_missing_usage_rejected(self):
        raw = {
            "model": "jev-latest",
            "answers": _noul_answers(),
        }
        with pytest.raises(JevResponseError):
            validate_response(raw, _noul_questions())

    def test_missing_model_rejected(self):
        raw = {
            "answers": _noul_answers(),
            "usage": {"input_tokens": 10, "output_tokens": 6},
        }
        with pytest.raises(JevResponseError):
            validate_response(raw, _noul_questions())

    def test_missing_answers_object_rejected(self):
        raw = {
            "model": "jev-latest",
            "usage": {"input_tokens": 10, "output_tokens": 6},
        }
        with pytest.raises(JevResponseError):
            validate_response(raw, _noul_questions())

    def test_empty_questions_rejected(self):
        raw = {
            "model": "jev-latest",
            "answers": {},
            "usage": {"input_tokens": 10, "output_tokens": 6},
        }
        with pytest.raises(JevResponseError):
            validate_response(raw, {})

    def test_empty_score_rubric_rejected(self):
        questions = {"q": Score(criteria=[], instructions="Empty rubric")}
        answers = {
            "q": {
                "type": "score",
                "score": 0.0,
                "confidence": 0.8,
                "probabilities": {},
                "legend": {},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), questions)

    def test_score_out_of_range_rejected(self):
        questions = {"q": Score(criteria=["low", "high"], instructions="Rate")}
        answers = {
            "q": {
                "type": "score",
                "score": 5.0,
                "confidence": 0.8,
                "probabilities": {0: 0.5, 1: 0.5},
                "legend": {0: "low", 1: "high"},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), questions)

    def test_negative_usage_tokens_rejected(self):
        raw = {
            "model": "jev-latest",
            "answers": _noul_answers(),
            "usage": {"input_tokens": -5, "output_tokens": 6},
        }
        with pytest.raises(JevResponseError):
            validate_response(raw, _noul_questions())

    def test_invalid_confidence_rejected(self):
        questions = _choice_questions()
        answers = {
            "primary": {
                "type": "choice",
                "choice": "a.md",
                "confidence": 1.5,
                "probabilities": {"a.md": 0.9, "b.md": 0.1},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_response(answers), questions)