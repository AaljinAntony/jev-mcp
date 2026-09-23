"""Deterministic decision policy: confidence, actions, escape hatches.

Borrowed from `reference/burnigtm-jev-mcp/src/policy.ts` and
`reference/jkudish-jev-mcp/src/lib.ts` (MIT). All arithmetic is local and
unit-testable; nothing here touches the network.
"""

from typing import Iterable, List, Literal

from jev_errors import JevValidationError

PolicyAction = Literal["auto", "review", "escalate"]

DEFAULT_AUTO_ACCEPT = 0.8
DEFAULT_REVIEW_AT = 0.5

#: Float-safe sum tolerance: an exact 0.01 delta can exceed 0.01 in IEEE-754.
PROBABILITY_SUM_TOLERANCE = 0.01 + 1e-12
#: Covers two-decimal score and probability reporting: 0.005 drift on the
#: score itself plus 0.005 * (0 + 1 + 2) = 0.015 on the distribution mean.
SCORE_MEAN_TOLERANCE = 0.02 + 1e-12

#: Escape-hatch options appended to Choice criteria so the model can decline
#: to pick a supplied candidate (jkudish `DECIDE_ESCAPE_HATCHES`).
ESCAPE_HATCHES = {
    "ask_user": "A consequential user preference or requirement is missing; ask instead of inventing it",
    "investigate": "Gather missing technical or factual evidence before selecting a candidate",
    "none": "None of the supplied candidates fits the known requirements",
}


def validate_policy_thresholds(auto_accept: float, review_at: float) -> None:
    """Thresholds must satisfy ``0 <= review_at <= auto_accept <= 1``."""
    if (
        not _is_finite(auto_accept)
        or not _is_finite(review_at)
        or auto_accept < 0
        or auto_accept > 1
        or review_at < 0
        or review_at > 1
        or review_at > auto_accept
    ):
        raise JevValidationError("Thresholds must satisfy 0 <= review_at <= auto_accept <= 1.")


def confidence_from_probabilities(probabilities) -> float:
    """Normalized confidence from a probability distribution.

    ``(max - 1/n) / (1 - 1/n)``; 0 when uniform or empty.
    """
    if not probabilities:
        return 0.0
    values = list(probabilities.values())
    if not values:
        return 0.0
    n = len(values)
    if n <= 1:
        return 1.0 if values[0] >= 1.0 else 0.0
    top = max(values)
    uniform = 1.0 / n
    if top <= uniform:
        return 0.0
    return (top - uniform) / (1.0 - uniform)


def action_from_confidence(
    confidence: float,
    auto_accept: float = DEFAULT_AUTO_ACCEPT,
    review_at: float = DEFAULT_REVIEW_AT,
) -> PolicyAction:
    """Map confidence to ``auto | review | escalate``."""
    validate_policy_thresholds(auto_accept, review_at)
    if not _is_finite(confidence):
        return "escalate"
    if confidence >= auto_accept:
        return "auto"
    if confidence >= review_at:
        return "review"
    return "escalate"


def worst_action(actions: Iterable[PolicyAction]) -> PolicyAction:
    """Combining independent judgments never softens the strictest one."""
    flattened = list(actions)
    if "escalate" in flattened:
        return "escalate"
    if "review" in flattened:
        return "review"
    return "auto"


def require_complete_context(action: PolicyAction, truncated: bool) -> PolicyAction:
    """Truncated input is incomplete context; it never permits ``auto``."""
    return "review" if (truncated and action == "auto") else action


def min_confidence(values: List) -> float:
    """Minimum present numeric confidence; 0 when none are present."""
    present = [float(v) for v in values if isinstance(v, (int, float))]
    if not present:
        return 0.0
    return min(present)


def guardrail_safe(
    action: PolicyAction,
    destructive_prob: float,
    git_modify_prob: float,
    destructive_threshold: float = 0.20,
    git_threshold: float = 0.20,
) -> bool:
    """Backward-compatible ``safe``: auto AND low destructive/git probabilities."""
    return (
        action == "auto"
        and destructive_prob < destructive_threshold
        and git_modify_prob < git_threshold
    )


def _is_finite(value: float) -> bool:
    try:
        import math

        return isinstance(value, (int, float)) and math.isfinite(value)
    except (TypeError, ValueError):
        return False