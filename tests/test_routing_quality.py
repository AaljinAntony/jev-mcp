"""Routing-quality gates over the labelled fixture set.

`scripts/eval_routing.py` is the measurement tool; this module is the gate. It
runs the harness once in mock mode (no key, no network, ~1 s) and asserts the
properties that must hold in *any* mode — one Jev round trip per tool call, a
self-consistent ranking, a false positive only where no file was expected, and
an input budget that has not silently doubled.

Accuracy floors are only meaningful against the real judge, so they are gated
behind `JEV_ROUTING_LIVE=1` plus a `TYPESAFE_API_KEY` and skipped otherwise.
Mock mode's accuracy is a stub's, not the model's.
"""

import os
import statistics
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.path.insert(0, str(REPO / "scripts"))


@pytest.fixture(scope="module")
def harness():
    import eval_routing

    return eval_routing


@pytest.fixture(scope="module")
def report(harness):
    import config
    import jev_engine

    os.environ["JEV_MCP_MOCK"] = "1"
    config._reset_config_cache()
    jev_engine._reset_client_cache()
    jev_engine._reset_settings_cache()
    return harness.evaluate(str(REPO), "mock")


@pytest.fixture(scope="module")
def cases(harness):
    return harness.load_cases()


# 1. The fixture set itself
def test_the_fixture_set_is_labelled_and_unique(cases):
    assert len(cases) >= 20, "the labelled set shrank; the gates below got weaker"
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "duplicate case ids would silently collapse the metrics"
    for case in cases:
        assert case.get("task"), f"{case['id']} has no task"
        # Every case declares an outcome, so "no file expected" is explicit
        # rather than inferred from a missing key.
        assert "expected_file" in case, f"{case['id']} does not say what to expect"
        if case["expected_file"] is not None:
            assert (REPO / case["expected_file"]).is_file(), (
                f"{case['id']} expects {case['expected_file']}, which is not in the workspace"
            )


def test_the_set_covers_both_outcomes(cases):
    file_cases = [c for c in cases if c["expected_file"]]
    null_cases = [c for c in cases if not c["expected_file"]]
    assert len(file_cases) >= 15, "too few positive cases to measure top-1 on"
    assert len(null_cases) >= 5, "too few negative cases for the false-positive rate to mean anything"


# 2. Cost invariants, which hold in every mode
def test_one_jev_round_trip_per_tool_call(report):
    """A second request per call doubles latency and spend; it must be visible."""
    summary, _ = report
    assert summary["requests_per_call"] == 1.0, (
        f"expected exactly one Jev request per tool call, got {summary['requests_per_call']}"
    )


def test_the_input_budget_has_not_regrown(report):
    _, rows = report
    tokens = [r["input_tokens"] for r in rows if r["input_tokens"]]
    assert tokens, "no usage was recorded; the harness is not measuring anything"
    # The offline judge reports `coverage.estimated_tokens.state + .questions`
    # from `fit_state`, so this is a real measurement of the payload the API
    # would receive, not a synthetic constant. `docs/perf-baseline.md` records
    # 12 885 tokens per call for `search_target_files` on this workspace at
    # MAX_TOTAL_PREVIEW_CHARS = 40 000; a regression to unbounded previews, or a
    # second question riding along, lands well above these bounds.
    mean = statistics.mean(tokens)
    assert mean < 15_000, f"mean input tokens rose to {mean:.0f}"
    assert max(tokens) < 20_000, f"worst-case input tokens rose to {max(tokens)}"


# 3. The reported rows must be internally consistent
def test_rows_agree_with_the_summary(report):
    summary, rows = report
    assert summary["cases"] == len(rows)
    file_rows = [r for r in rows if r["expected_file"]]
    null_rows = [r for r in rows if not r["expected_file"]]
    assert summary["top1_accuracy"] == pytest.approx(
        sum(1 for r in file_rows if r["hit1"]) / len(file_rows), abs=1e-4
    )
    assert summary["false_positive_rate"] == pytest.approx(
        sum(1 for r in null_rows if r["false_positive"]) / len(null_rows), abs=1e-4
    )


def test_a_hit_is_always_a_match_and_always_ranked_first(report):
    """`hit1` and `hit3` are derived from the same fields the tools returned.

    If the tool reported a file it did not rank first, the recall number would
    be measuring something other than what the tool did.
    """
    _, rows = report
    for row in rows:
        if row["hit1"]:
            assert row["top1_file"] == row["expected_file"]
            assert row["exists"] == "answered", f"{row['id']} hit top-1 but reports {row['exists']}"
        if row["false_positive"]:
            assert row["expected_file"] is None
            assert row["exists"] in {"answered", "partial"}


def test_no_case_reports_a_model_but_no_usage(report):
    _, rows = report
    for row in rows:
        if row["hit1"] or row["false_positive"]:
            assert row["input_tokens"] is not None, f"{row['id']} produced no usage"


# 4. Skills routing is deterministic offline, so it is gated in every mode
def test_skill_routing_is_exact_in_mock_mode(report):
    """The offline judge picks by keyword, and the fixtures were built for it."""
    summary, _ = report
    assert summary["skill_top1_accuracy"] == 1.0, (
        "the mock judge regressed on the skill set; Phase 3 promised exact top-1 there"
    )


# 5. Accuracy floors, live only
@pytest.mark.skipif(
    os.environ.get("JEV_ROUTING_LIVE") != "1" or not os.environ.get("TYPESAFE_API_KEY"),
    reason="live routing gates need JEV_ROUTING_LIVE=1 and TYPESAFE_API_KEY (costs one round trip per case)",
)
def test_live_accuracy_floors(harness):
    import config
    import jev_engine
    from config import ensure_dotenv

    ensure_dotenv()
    os.environ["JEV_MCP_MOCK"] = "0"
    config._reset_config_cache()
    jev_engine._reset_client_cache()
    summary, _ = harness.evaluate(str(REPO), "live")
    assert summary["top1_accuracy"] >= 0.70, f"top-1 fell to {summary['top1_accuracy']}"
    assert summary["top3_recall"] >= 0.85, f"top-3 fell to {summary['top3_recall']}"
    assert summary["false_positive_rate"] <= 0.20, (
        f"false positives rose to {summary['false_positive_rate']}"
    )
    assert summary["requests_per_call"] == 1.0
