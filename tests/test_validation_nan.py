"""Tests for NaN/Inf rejection in fail-closed response validation and serialization boundary."""

import json
import pytest

from typesafe_sdk import Choice, Noul, Score, SystemOneResponse, Usage
from jev_errors import JevResponseError
from jev_validation import (
    _assert_finite_json,
    _is_finite_number,
    validate_response,
)


def _base_response(answers, model="jev-latest", usage=None):
    if usage is None:
        usage = Usage(input_tokens=10, output_tokens=6)
    try:
        return SystemOneResponse(model=model, answers=answers, usage=usage)
    except Exception:
        return {"model": model, "answers": answers, "usage": usage}


class TestIsFiniteNumber:
    def test_finite_floats_and_ints(self):
        assert _is_finite_number(0) is True
        assert _is_finite_number(1) is True
        assert _is_finite_number(-42) is True
        assert _is_finite_number(0.5) is True
        assert _is_finite_number(1e-12) is True

    def test_non_finite_rejected(self):
        assert _is_finite_number(float("nan")) is False
        assert _is_finite_number(float("inf")) is False
        assert _is_finite_number(float("-inf")) is False

    def test_bools_rejected(self):
        # bool is subclass of int in Python: isinstance(True, int) is True
        assert _is_finite_number(True) is False
        assert _is_finite_number(False) is False

    def test_non_numbers_rejected(self):
        assert _is_finite_number("123") is False
        assert _is_finite_number(None) is False
        assert _is_finite_number([]) is False
        assert _is_finite_number({}) is False


class TestNanInfRejectionInValidation:
    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf"), True, False])
    def test_noul_rejects_non_finite_and_bool(self, bad_val):
        questions = {"dest": Noul(instructions="Is it destructive?")}
        answers = {"dest": {"type": "noul", "noul": bad_val}}
        with pytest.raises(JevResponseError):
            validate_response(_base_response(answers), questions)

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf"), True, False])
    def test_confidence_rejects_non_finite_and_bool(self, bad_val):
        questions = {"c": Choice(criteria={"a": "opt a", "b": "opt b"}, instructions="Pick")}
        answers = {
            "c": {
                "type": "choice",
                "choice": "a",
                "confidence": bad_val,
                "probabilities": {"a": 0.8, "b": 0.2},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_base_response(answers), questions)

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf"), True, False])
    def test_choice_probabilities_rejects_non_finite_and_bool(self, bad_val):
        questions = {"c": Choice(criteria={"a": "opt a", "b": "opt b"}, instructions="Pick")}
        answers = {
            "c": {
                "type": "choice",
                "choice": "a",
                "confidence": 0.8,
                "probabilities": {"a": bad_val, "b": 0.2},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_base_response(answers), questions)

    def test_choice_probabilities_sum_check_rejects_nan(self):
        # A single NaN in probabilities causes total sum to be NaN
        questions = {"c": Choice(criteria={"a": "opt a", "b": "opt b"}, instructions="Pick")}
        # Even if a single value somehow produced NaN sum:
        answers = {
            "c": {
                "type": "choice",
                "choice": "a",
                "confidence": 0.5,
                "probabilities": {"a": float("nan"), "b": float("nan")},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_base_response(answers), questions)

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf"), True, False])
    def test_score_rejects_non_finite_and_bool(self, bad_val):
        questions = {"s": Score(criteria=["low", "mid", "high"], instructions="Rate")}
        answers = {
            "s": {
                "type": "score",
                "score": bad_val,
                "confidence": 0.9,
                "probabilities": {0: 0.1, 1: 0.8, 2: 0.1},
                "legend": {0: "low", 1: "mid", 2: "high"},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_base_response(answers), questions)

    @pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf"), True, False])
    def test_score_probabilities_rejects_non_finite_and_bool(self, bad_val):
        questions = {"s": Score(criteria=["low", "mid", "high"], instructions="Rate")}
        answers = {
            "s": {
                "type": "score",
                "score": 1.0,
                "confidence": 0.9,
                "probabilities": {0: bad_val, 1: 0.8, 2: 0.1},
                "legend": {0: "low", 1: "mid", 2: "high"},
            }
        }
        with pytest.raises(JevResponseError):
            validate_response(_base_response(answers), questions)


class TestAssertFiniteJson:
    def test_assert_finite_json_passes_valid_nested_dict(self):
        data = {
            "safe": True,
            "count": 5,
            "name": "test",
            "score": 0.95,
            "coverage": {
                "original_chars": 100,
                "estimated_tokens": {"state": 10, "questions": 5},
            },
            "ranked": [{"file": "a.py", "probability": 0.8}],
        }
        _assert_finite_json(data)  # Should not raise

    def test_assert_finite_json_catches_nested_nan(self):
        data = {
            "result": {
                "coverage": {
                    "estimated_tokens": {
                        "state": float("nan"),
                    }
                }
            }
        }
        with pytest.raises(JevResponseError) as exc_info:
            _assert_finite_json(data)
        assert "result.coverage.estimated_tokens.state" in str(exc_info.value)

    def test_assert_finite_json_catches_nested_inf(self):
        data = {"values": [1.0, float("inf"), 3.0]}
        with pytest.raises(JevResponseError) as exc_info:
            _assert_finite_json(data)
        assert "result.values[1]" in str(exc_info.value)

    def test_json_dumps_never_emits_nan_when_validated(self):
        envelope = {
            "action": "auto",
            "confidence": 0.85,
            "coverage": {
                "complete": True,
                "estimated_tokens": {"state": 12},
            },
        }
        _assert_finite_json(envelope)
        serialized = json.dumps(envelope)
        assert "NaN" not in serialized
        assert "Infinity" not in serialized
