"""Deterministic decision policy: confidence, actions, escape hatches.

Borrowed from `reference/burnigtm-jev-mcp/src/policy.ts` and
`reference/jkudish-jev-mcp/src/lib.ts` (MIT). All arithmetic is local and
unit-testable; nothing here touches the network.
"""

import math
from typing import Iterable, List, Literal

from jev_errors import JevValidationError

PolicyAction = Literal["auto", "review", "escalate"]

DEFAULT_AUTO_ACCEPT = 0.8
DEFAULT_REVIEW_AT = 0.5
DEFAULT_RISK_THRESHOLD = 0.20
DEFAULT_ESCALATE_THRESHOLD = 0.50

#: Presence probability at or above which a chosen candidate is reported as a
#: real match. Below it the Choice winner is treated as a forced pick among
#: options that do not actually fit, which is the failure mode a `none` option
#: inside a Choice cannot detect on its own.
NONE_CONFIDENCE = 0.50

#: Minimum primary probability before family-prefix siblings are pulled in.
#: A 0.15-probability primary is a guess, not a decision, and must not be
#: allowed to fill every result slot with its own directory family.
FAMILY_CLUSTER_MIN_PROB = 0.50
#: Hard cap on siblings added by family-prefix clustering.
FAMILY_CLUSTER_MAX_SIBLINGS = 2

#: Float-safe sum tolerance: an exact 0.01 delta can exceed 0.01 in IEEE-754.
PROBABILITY_SUM_TOLERANCE = 0.01 + 1e-12
#: Per-level drift allowed between a reported score and its distribution mean.
#: Two-decimal reporting gives 0.005 on the score and 0.005 on each level's
#: probability, so the total drifts by 0.005 * (1 + sum of levels).
SCORE_MEAN_PER_LEVEL_TOLERANCE = 0.01


def score_mean_tolerance(levels: int) -> float:
    """Absolute tolerance between `score` and its distribution mean.

    Scaled by rubric size: a 7-level rubric accumulates more rounding drift than
    a 2-level one, and an absolute cap rejects valid long-rubric responses.
    Mirrors reference/burnigtm-jev-mcp/src/responses.ts:39.
    """
    if levels <= 0:
        return 0.0
    return SCORE_MEAN_PER_LEVEL_TOLERANCE * (levels - 1) + 1e-12


#: Backwards-compatibility alias for 3-level rubrics.
SCORE_MEAN_TOLERANCE = score_mean_tolerance(3)

# NOTE: the escape-hatch vocabulary ("ask_user" / "investigate" / "none") from
# jkudish's `DECIDE_ESCAPE_HATCHES` used to live here as `ESCAPE_HATCHES`. It was
# never read: only "none" was ever offered, and it is a `criteria` entry each tool
# adds to its own Choice (see `search_target_files` and `AGENT_RESOURCE_
# INSTRUCTIONS`). An unused dict invites the belief that asking the user or
# investigating is enforced somewhere, and nothing in this file enforced it.
# `ask_user` and `investigate` remain unimplemented on purpose: each new Choice
# option changes the model's behaviour and needs the labelled routing set in
# `scripts/eval_routing.py` re-run before it ships.


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
    destructive_threshold: float = DEFAULT_RISK_THRESHOLD,
    git_threshold: float = DEFAULT_RISK_THRESHOLD,
) -> bool:
    """Backward-compatible ``safe``: auto AND low destructive/git probabilities."""
    return (
        action == "auto"
        and destructive_prob < destructive_threshold
        and git_modify_prob < git_threshold
    )


def _is_finite(value: float) -> bool:
    try:
        return isinstance(value, (int, float)) and math.isfinite(value)
    except (TypeError, ValueError):
        return False