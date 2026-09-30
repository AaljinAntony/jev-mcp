"""The mock judge must get faster without changing a single judgment.

`mock_system_one` is what the test suite, the demos and `JEV_MCP_MOCK=1` all run
on, so its answers are pinned here. The optimization under test is threading one
tokenization of the state through every question instead of re-deriving it per
option; if it ever changes a probability, the golden below fails.
"""

import math

from typesafe_sdk import Choice, Noul, Score

import mock
from limits import estimate_tokens

STATE = "Task: refactor python code and add type annotations to improve system reliability."

#: 250 options, of which a few carry task-specific words and the rest are noise.
CRITERIA = {
    f".agents/skills/skill_{i:03d}/SKILL.md": (
        f"Agent resource: skill_{i:03d}.md — refactor the python database layer and add type annotations"
        if i % 7 == 0
        else f"Agent resource: skill_{i:03d}.md — unrelated notes about release notes drafting"
    )
    for i in range(250)
}


def _questions():
    return {
        "primary": Choice(criteria=CRITERIA, instructions="Select the primary matching agent skill."),
        "secondary": Choice(criteria={**CRITERIA, "none": "No additional relevant resource"},
                            instructions="Select secondary."),
        "presence": Noul(
            instructions="Does at least one supplied file actually have to be inspected?",
            criteria={"true": "yes", "false": "no"},
        ),
        "risky": Noul(instructions="Does this command permanently delete files or wipe directories?"),
        "benign": Noul(instructions="Is this a read-only git command?"),
        "level": Score(
            instructions="Rate the refactor complexity.",
            criteria=["trivial", "moderate", "multi-file"],
        ),
    }


#: Recorded from the pre-Phase-5 implementation and re-verified against it by
#: running both modules over this exact request (see docs/perf-baseline.md).
GOLDEN = {
    "primary": {
        "choice": ".agents/skills/skill_000/SKILL.md",
        "confidence": 0.0007705631948771603,
        "probability_count": 250,
        "top_probabilities": [
            [".agents/skills/skill_000/SKILL.md", 0.00476748],
            [".agents/skills/skill_007/SKILL.md", 0.00476748],
            [".agents/skills/skill_014/SKILL.md", 0.00476748],
        ],
    },
    "secondary": {
        "choice": "none",
        "confidence": 0.008844734739034422,
        "probability_count": 251,
        "top_probabilities": [
            ["none", 0.01279356],
            [".agents/skills/skill_000/SKILL.md", 0.00470649],
            [".agents/skills/skill_007/SKILL.md", 0.00470649],
        ],
    },
    "presence": {"noul": 0.1},
    "risky": {"noul": 0.35},
    "benign": {"noul": 0.35},
    "level": {
        "score": 1.0220943786736978,
        "confidence": 0.013878410348689583,
        "probability_count": 3,
        "top_probabilities": [
            ["2", 0.34258561],
            ["1", 0.33692316],
            ["0", 0.32049123],
        ],
        "legend": {"0": "trivial", "1": "moderate", "2": "multi-file"},
    },
    "model": "jev-latest+mock",
    "output_tokens": 48,
}


def _observed(res):
    out = {}
    for name in ("primary", "secondary", "presence", "risky", "benign", "level"):
        ans = res.answers[name]
        entry = {}
        for attr in ("choice", "noul", "score", "confidence"):
            if hasattr(ans, attr):
                entry[attr] = getattr(ans, attr)
        probs = getattr(ans, "probabilities", None)
        if probs:
            top = sorted(dict(probs).items(), key=lambda kv: -float(kv[1]))[:3]
            entry["probability_count"] = len(probs)
            entry["top_probabilities"] = [[str(k), round(float(v), 8)] for k, v in top]
            legend = getattr(ans, "legend", None)
            if legend:
                entry["legend"] = {str(k): v for k, v in sorted(dict(legend).items())}
        out[name] = entry
    out["model"] = res.model
    out["output_tokens"] = res.usage.output_tokens
    return out


#: The golden below was recorded on Windows. Summing the inverse-frequency
#: weights walks a dict, and CPython's float accumulation order differs between
#: builds and platforms, so a value can differ in the last two or three digits
#: of a 17-significant-digit float — observed 0.0007705631948771438 on Linux
#: against ...1603 on Windows. Comparing those with `==` makes the pin fail on
#: any platform but the one that recorded it, which pins the test to a machine
#: instead of to a behaviour.
#:
#: Rounding both sides to 12 decimal places still catches every change that
#: matters — a real behavioural drift moves a probability or swaps a choice, by
#: orders of magnitude more than 1e-12 — while tolerating the float noise.
GOLDEN_DECIMALS = 12


def _pinned(obj):
    """Recursively round every float so the comparison is platform-stable."""
    if isinstance(obj, float):
        return round(obj, GOLDEN_DECIMALS)
    if isinstance(obj, dict):
        return {k: _pinned(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_pinned(v) for v in obj]
    return obj


class TestMockOutputIsPinned:
    def test_250_option_request_matches_the_golden_answers(self):
        res = mock.mock_system_one(STATE, _questions())
        assert _pinned(_observed(res)) == _pinned(GOLDEN)

    def test_repeated_calls_are_identical(self):
        first = _observed(mock.mock_system_one(STATE, _questions()))
        second = _observed(mock.mock_system_one(STATE, _questions()))
        assert _pinned(first) == _pinned(second) == _pinned(GOLDEN)


class TestStateIsTokenizedOnce:
    def test_token_set_called_once_for_250_options_and_6_questions(self, monkeypatch):
        calls = []
        real = mock._token_set

        def _spy(text):
            calls.append(text)
            return real(text)

        monkeypatch.setattr(mock, "_token_set", _spy)
        mock.mock_system_one(STATE, _questions())
        assert len(calls) == 1, f"state re-tokenized {len(calls)} times"

    def test_state_terms_derived_not_recomputed(self, monkeypatch):
        calls = []
        real = mock._state_terms

        def _spy(text):
            calls.append(text)
            return real(text)

        monkeypatch.setattr(mock, "_state_terms", _spy)
        mock.mock_system_one(STATE, _questions())
        # `_state_terms` is only the no-precomputed-set fallback; the threaded
        # path must not need it at all.
        assert calls == []

    def test_overlap_set_matches_the_legacy_two_text_form(self):
        left = mock._token_set(STATE)
        for other in ("type annotations python", "release notes", "", "1234", "a b c"):
            assert mock._overlap_set(left, other) == mock._overlap(STATE, other)


class TestPresenceNoul:
    def test_presence_question_uses_evidence_not_command_regexes(self):
        options = list(CRITERIA.values())
        frequency = mock._document_frequency(options)
        value = mock._mock_noul(
            "delete nothing in particular; refactor python",
            "Is a supplied file genuinely required?",
            presence=True,
            options=options,
            frequency=frequency,
        )
        assert 0.0 < value < 1.0
        assert value != 0.97

    def test_benign_command_still_scores_low(self):
        assert mock._mock_noul("git status", "Does this modify anything?") == 0.03

    def test_destructive_command_still_scores_high(self):
        assert mock._mock_noul("rm -rf /tmp/scratch", "Is this safe?") == 0.97

    def test_destructive_command_wins_over_the_presence_branch(self):
        # `verify_command` asks exactly these two questions; the guardrail must
        # never be diluted by a presence-style instruction.
        assert mock._mock_noul(
            "Terminal shell command to execute: rm -rf /tmp/x",
            "Does this command permanently delete files?",
            presence=False,
        ) == 0.97


class TestInputTokensAreReused:
    def test_supplied_count_is_used_verbatim(self):
        res = mock.mock_system_one(STATE, _questions(), input_tokens=1234)
        assert res.usage.input_tokens == 1234

    def test_supplied_count_is_what_fit_state_measured(self):
        from limits import fit_state

        questions = _questions()
        fitted = fit_state(STATE, questions)
        counts = fitted["coverage"]["estimated_tokens"]
        res = mock.mock_system_one(
            STATE, questions, input_tokens=counts["state"] + counts["questions"]
        )
        assert res.usage.input_tokens == counts["state"] + counts["questions"]
        # The old implementation re-serialized the combined dict, which includes
        # the question key names and JSON punctuation on top of the values that
        # `fit_state` sums. Same magnitude, slightly smaller now.
        combined_dict = estimate_tokens({"state": STATE, "questions": questions})
        assert 0.9 <= res.usage.input_tokens / combined_dict <= 1.0

    def test_falls_back_to_estimation_when_unsupplied(self):
        res = mock.mock_system_one(STATE, _questions())
        assert res.usage.input_tokens > 0
        assert math.isfinite(res.usage.input_tokens)
