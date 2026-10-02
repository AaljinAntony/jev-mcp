import pytest

from jev_errors import JevValidationError
from policy import (
    DEFAULT_AUTO_ACCEPT,
    DEFAULT_ESCALATE_THRESHOLD,
    DEFAULT_REVIEW_AT,
    DEFAULT_RISK_THRESHOLD,
    action_from_confidence,
    confidence_from_probabilities,
    guardrail_safe,
    require_complete_context,
    score_mean_tolerance,
    validate_policy_thresholds,
    worst_action,
)


class TestScoreMeanTolerance:
    def test_zero_or_negative_levels(self):
        assert score_mean_tolerance(0) == 0.0
        assert score_mean_tolerance(-1) == 0.0

    def test_tolerance_scaling(self):
        assert score_mean_tolerance(1) == pytest.approx(1e-12)
        assert score_mean_tolerance(2) == pytest.approx(0.01 + 1e-12)
        assert score_mean_tolerance(3) == pytest.approx(0.02 + 1e-12)
        assert score_mean_tolerance(7) == pytest.approx(0.06 + 1e-12)


class TestConfidenceFromProbabilities:
    def test_uniform_is_zero(self):
        assert confidence_from_probabilities({"a": 0.5, "b": 0.5}) == 0.0

    def test_empty_is_zero(self):
        assert confidence_from_probabilities({}) == 0.0

    def test_peak_formula(self):
        # (0.9 - 0.5) / (1 - 0.5) = 0.8
        assert confidence_from_probabilities({"a": 0.9, "b": 0.1}) == pytest.approx(0.8)

    def test_three_way_peak(self):
        # (0.8 - 1/3) / (1 - 1/3) = 0.7
        assert confidence_from_probabilities({"a": 0.8, "b": 0.1, "c": 0.1}) == pytest.approx(0.7)

    def test_single_slot(self):
        assert confidence_from_probabilities({"a": 1.0}) == 1.0
        assert confidence_from_probabilities({"a": 0.4}) == 0.0


class TestActionFromConfidence:
    def test_defaults_auto_review_escalate(self):
        assert action_from_confidence(0.9) == "auto"
        assert action_from_confidence(0.6) == "review"
        assert action_from_confidence(0.2) == "escalate"

    def test_custom_thresholds(self, monkeypatch):
        assert action_from_confidence(0.9, auto_accept=0.95, review_at=0.5) == "review"
        assert action_from_confidence(0.96, auto_accept=0.95, review_at=0.5) == "auto"

    def test_non_finite_escalates(self):
        assert action_from_confidence(float("nan")) == "escalate"

    def test_boundaries(self):
        assert action_from_confidence(DEFAULT_AUTO_ACCEPT) == "auto"
        assert action_from_confidence(DEFAULT_REVIEW_AT) == "review"


class TestThresholdValidation:
    def test_valid(self):
        validate_policy_thresholds(0.8, 0.5)

    def test_inverted_raises(self):
        with pytest.raises(JevValidationError):
            validate_policy_thresholds(0.4, 0.6)

    def test_out_of_range_raises(self):
        with pytest.raises(JevValidationError):
            validate_policy_thresholds(1.2, 0.5)
        with pytest.raises(JevValidationError):
            validate_policy_thresholds(0.8, -0.1)


class TestWorstAction:
    def test_severity_ordering(self):
        assert worst_action(["auto", "review"]) == "review"
        assert worst_action(["review", "auto", "escalate"]) == "escalate"
        assert worst_action(["auto", "auto"]) == "auto"
        assert worst_action([]) == "auto"


class TestRequireCompleteContext:
    def test_truncated_never_auto(self):
        assert require_complete_context("auto", True) == "review"
        assert require_complete_context("review", True) == "review"
        assert require_complete_context("escalate", True) == "escalate"

    def test_complete_unchanged(self):
        assert require_complete_context("auto", False) == "auto"


class TestGuardrailSafe:
    def test_auto_only_when_below_thresholds(self):
        assert guardrail_safe("auto", 0.03, 0.03) is True
        assert guardrail_safe("auto", 0.30, 0.03) is False
        assert guardrail_safe("auto", 0.03, 0.30) is False
        assert guardrail_safe("review", 0.03, 0.03) is False
        assert guardrail_safe("escalate", 0.03, 0.03) is False

    def test_risk_constants_and_boundaries(self):
        assert DEFAULT_RISK_THRESHOLD == 0.20
        assert DEFAULT_ESCALATE_THRESHOLD == 0.50
        assert guardrail_safe("auto", 0.199, 0.199) is True
        assert guardrail_safe("auto", 0.20, 0.0) is False


def test_no_escape_hatch_dict_remains():
    """`ESCAPE_HATCHES` was defined and never read.

    A policy constant nobody enforces is worse than no constant: the next
    reader assumes `ask_user` and `investigate` are offered and that a `none`
    selection is being routed somewhere. The only escape hatch in use is the
    literal `"none"` criterion, which each tool adds to its own Choice — and
    that lives in the tools, not in policy.
    """
    import policy

    assert not hasattr(policy, "ESCAPE_HATCHES")
    assert "ask_user" not in dir(policy)