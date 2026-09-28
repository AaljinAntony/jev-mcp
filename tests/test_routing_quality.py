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


@pytest.fixture(scope="module")
def mcp_report(harness):
    import config
    import jev_engine

    os.environ["JEV_MCP_MOCK"] = "1"
    config._reset_config_cache()
    jev_engine._reset_client_cache()
    jev_engine._reset_settings_cache()
    return harness.evaluate_mcp("mock")


@pytest.fixture(scope="module")
def mcp_cases(harness):
    return harness.load_mcp_cases()


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


# 5. `select_mcp_tools`: same invariants, over its own fixture set
def test_the_mcp_fixture_set_is_labelled(mcp_cases):
    assert len(mcp_cases) >= 10, "the MCP set shrank; the gates below got weaker"
    ids = [c["id"] for c in mcp_cases]
    assert len(set(ids)) == len(ids)
    for case in mcp_cases:
        assert case.get("task"), f"{case['id']} has no task"
        assert "expected_mcp" in case, f"{case['id']} does not say what to expect"
        assert case.get("roster"), f"{case['id']} supplies no roster"
        for server in case["roster"]:
            assert isinstance(server.get("name"), str) and server["name"]


def test_the_mcp_set_covers_both_outcomes_and_a_tie(mcp_cases):
    assert sum(1 for c in mcp_cases if c.get("expected_mcp")) >= 6
    assert sum(1 for c in mcp_cases if not c.get("expected_mcp")) >= 2, (
        "without negative cases the false-positive rate cannot mean anything"
    )
    assert sum(1 for c in mcp_cases if c.get("expected_mcp_ambiguous")) >= 1, (
        "the ambiguous verdict needs a labelled tie or it is never measured"
    )


def test_the_judge_never_selects_itself(mcp_report):
    """The anti-loop invariant, in every mode.

    A judge that recommends `jev-engine` sends the agent back into the judge.
    One occurrence is a broken exclusion, not a low score.
    """
    summary, rows = mcp_report
    assert summary["mcp_judge_picks"] == 0, (
        f"the judge was selected on {[r['id'] for r in rows if r['judge_picked']]}"
    )
    for row in rows:
        assert row["top1_mcp"] != "jev-engine"


def test_one_jev_round_trip_per_mcp_call(mcp_report):
    summary, _ = mcp_report
    assert summary["mcp_requests_per_call"] == 1.0


def test_the_mcp_input_budget_is_bounded(mcp_report):
    """A roster is caller-supplied, so the ceiling is what keeps the cost sane."""
    summary, _ = mcp_report
    assert summary["mcp_input_tokens_per_call"], "the harness measured nothing"
    assert summary["mcp_input_tokens_per_call"] < 6_000, (
        f"mean input tokens rose to {summary['mcp_input_tokens_per_call']}"
    )
    assert summary["mcp_input_tokens_median"] < 6_000


def test_the_offline_judge_never_invents_a_server(mcp_cases, mcp_report):
    """A false positive sends the agent to an MCP the task never needed.

    In mock mode this is gated exactly: the offline judge is a keyword matcher
    and a recommendation it invented would be the stub's bug, not the model's.
    """
    _, rows = mcp_report
    assert not [r["id"] for r in rows if r["mcp_false_positive"]], (
        "the offline judge claimed a server for a task that needs none"
    )
    for row in rows:
        if not row["expected_mcp"]:
            assert row["exists"] in {"absent", "partial"}, (
                f"{row['id']} reports {row['exists']} for a task that needs no server"
            )


def test_an_answered_verdict_never_looks_like_call_nothing(mcp_report):
    """An empty tool list must be announced, not passed off as a selection.

    `answered` with no tool is legitimate — the server is right and none of its
    tools cleared the floor — because the agent still holds the roster. It just
    may not read as "this is the tool to call".
    """
    _, rows = mcp_report
    for row in rows:
        if row["exists"] == "answered" and not row["tools"]:
            assert "no_tools_above_threshold" in row["reason_codes"], (
                f"{row['id']} is answered with no tool and never said so"
            )
        if row["exists"] != "answered":
            assert row["top1_mcp"] is None, f"{row['id']} reported {row['top1_mcp']} without answering"


# 6. Accuracy floors, live only
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


@pytest.mark.skipif(
    os.environ.get("JEV_ROUTING_LIVE") != "1" or not os.environ.get("TYPESAFE_API_KEY"),
    reason="live MCP gates need JEV_ROUTING_LIVE=1 and TYPESAFE_API_KEY",
)
def test_live_mcp_accuracy_floors(harness):
    """`mcp_acceptable_rate`, not strict top-1: a two-server tie is not a miss."""
    import config
    import jev_engine
    from config import ensure_dotenv

    ensure_dotenv()
    os.environ["JEV_MCP_MOCK"] = "0"
    config._reset_config_cache()
    jev_engine._reset_client_cache()
    summary, rows = harness.evaluate_mcp("live")
    assert summary["mcp_acceptable_rate"] >= 0.85, (
        f"only {summary['mcp_acceptable_rate']} of tasks routed to a usable server"
    )
    assert summary["mcp_top1_accuracy"] >= 0.70, f"top-1 fell to {summary['mcp_top1_accuracy']}"
    assert summary["mcp_false_positive_rate"] <= 0.34, (
        f"false positives rose to {summary['mcp_false_positive_rate']}"
    )
    assert summary["mcp_judge_picks"] == 0
    assert summary["mcp_requests_per_call"] == 1.0
    # The tie must never become a confident pick of a server that does not fit.
    # Either it is surfaced as a tie, or the agent is handed one of the two
    # interchangeable servers, which is a decision it can act on. `ambiguous_recall`
    # itself is reported, not gated: one labelled tie cannot support a rate, and
    # a 0.47/0.40 split lands on either side of the gap threshold.
    tie = [r for r in rows if r["expected_ambiguous"]]
    assert tie, "the labelled tie case vanished from the set"
    for row in tie:
        assert row["ambiguous"] or row["top1_mcp"] in row["acceptable_mcps"], (
            f"{row['id']} is a tie but was reported as {row['exists']} -> {row['top1_mcp']}"
        )
