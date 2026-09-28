"""`select_mcp_tools`: the agent-facing MCP roster selector.

Three things here are the whole point of the tool and are tested as such:

* the judge is never a candidate — a recommendation of `jev-engine` sends the
  agent straight back into the judge, and that loop does not terminate;
* "several MCP servers are equally usable" is a verdict of its own, because a
  Choice always returns a winner and a flat distribution's winner is arbitrary;
* the tool list is "every tool that cleared the threshold", and anything the
  caller's ceiling or the option cap removed is reported rather than dropped.
"""

import json
from pathlib import Path

import pytest

from typesafe_sdk import ChoiceAnswer, NoulAnswer, SystemOneResponse, Usage

REPO = Path(__file__).resolve().parent.parent

import jev_engine
from jev_errors import JevValidationError


def _server(name, description="", tools=()):
    return {
        "name": name,
        "description": description,
        "tools": [{"name": tool, "description": f"do {tool}"} for tool in tools],
    }


ROSTER = [
    _server("jev-engine", "Jev decision engine", ["guardrail_command"]),
    _server("git", "read and write git repositories", ["git_commit", "git_diff"]),
    _server("github", "github issues, pull requests, releases", ["create_pr"]),
]


def install(
    monkeypatch,
    server_probs,
    tool_probs=None,
    relevance=0.9,
    decisive=0.9,
    confidence=0.9,
    truncated=False,
):
    """Answer the four questions with hand-built distributions, no SDK call.

    The `questions` and `state` the tool actually built are captured, so a test
    can assert on the evidence the model was given rather than on the code that
    built it.
    """
    seen = {}

    def _fake_request(state, questions):
        seen["state"] = state
        seen["questions"] = questions
        probs = dict(tool_probs) if tool_probs else {"none": 1.0}
        answers = {
            "target_server": ChoiceAnswer(
                choice=max(server_probs, key=lambda k: server_probs[k]),
                probabilities=dict(server_probs),
                confidence=confidence,
            ),
            "is_relevant": NoulAnswer(noul=relevance),
            "is_decisive": NoulAnswer(noul=decisive),
        }
        if "target_tool" in questions:
            answers["target_tool"] = ChoiceAnswer(
                choice=max(probs, key=lambda k: probs[k]),
                probabilities=probs,
                confidence=0.9,
            )
        response = SystemOneResponse(
            model="jev-test",
            answers=answers,
            usage=Usage(input_tokens=40, output_tokens=12),
        )
        return (
            response,
            {"truncated": truncated, "coverage": {}},
            jev_engine.get_config(),
        )

    monkeypatch.setattr(jev_engine, "_request", _fake_request)
    return seen


def _server_probs(**kwargs):
    probs = {name: 0.0 for name in kwargs}
    probs["none"] = 0.0
    probs.update(kwargs)
    return probs


def _tool_probs(**kwargs):
    probs = {key: 0.0 for key in kwargs}
    probs["none"] = 0.0
    probs.update(kwargs)
    return probs


# 1. The judge is never a candidate
def test_the_judge_is_never_offered_as_a_candidate(monkeypatch, tmp_path):
    seen = install(monkeypatch, _server_probs(git=0.8, github=0.1, none=0.1))
    jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    server_criteria = seen["questions"]["target_server"].criteria
    tool_criteria = seen["questions"]["target_tool"].criteria
    assert not any("jev-engine" in key for key in server_criteria)
    assert not any("jev-engine" in key for key in tool_criteria)
    assert not any("jev-engine" in str(value) for value in server_criteria.values())


def test_a_suffixed_judge_instance_is_excluded_too(monkeypatch, tmp_path):
    """`jev-engine-local` is the same loop as `jev-engine`."""
    install(monkeypatch, _server_probs(git=0.9, none=0.1))
    res = jev_engine.select_mcp_tools(
        "commit the staged changes",
        [_server("jev-engine-local"), _server("JEV-ENGINE"), _server("git", tools=["git_commit"])],
        str(tmp_path),
    )
    assert res["exists"] == "answered"
    assert all(entry["server"] != "git" for entry in res["excluded"] if entry["reason"] == "self")
    assert {entry["server"] for entry in res["excluded"]} == {"jev-engine-local", "JEV-ENGINE"}
    assert all(entry["reason"] == "self" for entry in res["excluded"])


def test_the_judge_never_appears_in_the_result(monkeypatch, tmp_path):
    """Excluded is the one place its name may appear: dropped, but visible."""
    install(monkeypatch, _server_probs(git=0.7, github=0.2, none=0.1))
    res = jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert res["primary"]["server"] == "git"
    for field in ("servers", "tools", "ranked_tools"):
        assert "jev-engine" not in json.dumps(res[field], default=str)
    assert [entry for entry in res["excluded"] if entry["server"] == "jev-engine"] == [
        {"server": "jev-engine", "reason": "self"}
    ]


def test_only_the_judge_supplied_never_calls_the_provider(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(jev_engine, "_request", lambda *a, **k: calls.append(a))
    res = jev_engine.select_mcp_tools("do anything", [_server("jev-engine", tools=["x"])], str(tmp_path))
    assert calls == [], "a roster of nothing but the judge must not spend a round trip"
    assert res["exists"] == "no_candidates"
    assert res["matched"] is False
    assert res["action"] == "auto"
    assert res["confidence"] is None
    assert [entry["server"] for entry in res["excluded"]] == ["jev-engine"]
    assert "jev-engine" in res["summary"]


def test_an_empty_roster_is_a_result_not_an_error(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(jev_engine, "_request", lambda *a, **k: calls.append(a))
    res = jev_engine.select_mcp_tools("do anything", [], str(tmp_path))
    assert calls == []
    assert res["exists"] == "no_candidates"
    assert res["excluded"] == []


# 2. `ignore_mcps`: exact names and globs
def test_ignore_mcps_drops_configured_names(monkeypatch, tmp_path):
    monkeypatch.setattr(
        jev_engine,
        "load_jev_settings",
        lambda: {"ignore_mcps": ["git"]},
    )
    seen = install(monkeypatch, _server_probs(github=0.8, none=0.2))
    res = jev_engine.select_mcp_tools("open a pull request", ROSTER, str(tmp_path))
    assert "git" not in seen["questions"]["target_server"].criteria
    assert res["primary"]["server"] == "github"
    assert {"server": "git", "reason": "configured"} in res["excluded"]


def test_ignore_mcps_accepts_globs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        jev_engine,
        "load_jev_settings",
        lambda: {"ignore_mcps": ["g*hub", "playwright"]},
    )
    seen = install(monkeypatch, _server_probs(git=0.8, none=0.2))
    jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert set(seen["questions"]["target_server"].criteria) == {"git", "none"}


def test_ignore_mcps_is_case_insensitive(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_engine, "load_jev_settings", lambda: {"ignore_mcps": ["GIT"]})
    seen = install(monkeypatch, _server_probs(github=0.8, none=0.2))
    jev_engine.select_mcp_tools("open a pull request", ROSTER, str(tmp_path))
    assert "git" not in seen["questions"]["target_server"].criteria


def test_a_configured_ignore_of_the_judge_is_still_self(monkeypatch, tmp_path):
    """The reason code names the stronger rule when both would match."""
    monkeypatch.setattr(jev_engine, "load_jev_settings", lambda: {"ignore_mcps": ["jev-engine"]})
    install(monkeypatch, _server_probs(git=0.9, none=0.1))
    res = jev_engine.select_mcp_tools("commit", ROSTER, str(tmp_path))
    assert [entry["reason"] for entry in res["excluded"]] == ["self"]


# 3. Input normalisation
def test_a_json_string_roster_is_accepted(monkeypatch, tmp_path):
    seen = install(monkeypatch, _server_probs(git=0.9, none=0.1))
    res = jev_engine.select_mcp_tools("commit the staged changes", json.dumps(ROSTER), str(tmp_path))
    assert res["primary"]["server"] == "git"
    assert "github" in seen["questions"]["target_server"].criteria


def test_a_broken_json_string_is_refused(tmp_path):
    with pytest.raises(JevValidationError):
        jev_engine.select_mcp_tools("commit", "[{not json", str(tmp_path))


def test_a_single_object_is_accepted_as_a_one_server_roster(monkeypatch, tmp_path):
    install(monkeypatch, _server_probs(git=0.9, none=0.1))
    res = jev_engine.select_mcp_tools("commit", _server("git", tools=["git_commit"]), str(tmp_path))
    assert res["primary"]["server"] == "git"


def test_malformed_entries_are_dropped_and_reported(monkeypatch, tmp_path):
    install(monkeypatch, _server_probs(git=0.9, none=0.1))
    res = jev_engine.select_mcp_tools(
        "commit",
        ["not an object", {"description": "no name"}, {"name": "  "}, _server("git", tools=["git_commit"])],
        str(tmp_path),
    )
    assert res["primary"]["server"] == "git"
    assert [entry["reason"] for entry in res["excluded"]] == ["invalid"] * 3
    assert any(code.startswith("invalid_entries=3") for code in res["reason_codes"])


def test_a_roster_of_only_malformed_entries_is_refused(tmp_path):
    with pytest.raises(JevValidationError) as excinfo:
        jev_engine.select_mcp_tools("commit", ["nope", 7], str(tmp_path))
    assert "no usable" in str(excinfo.value).lower()


def test_duplicate_servers_collapse(monkeypatch, tmp_path):
    seen = install(monkeypatch, _server_probs(git=0.9, none=0.1))
    jev_engine.select_mcp_tools("commit", [_server("git"), _server("git")], str(tmp_path))
    assert "git" in seen["questions"]["target_server"].criteria
    assert list(seen["questions"]["target_server"].criteria).count("git") == 1


def test_a_tool_without_a_description_is_still_selectable(monkeypatch, tmp_path):
    seen = install(
        monkeypatch,
        _server_probs(git=0.9, none=0.1),
        _tool_probs(**{"git::commit": 0.9, "none": 0.1}),
    )
    res = jev_engine.select_mcp_tools(
        "commit", [{"name": "git", "tools": [{"name": "commit"}]}], str(tmp_path)
    )
    assert "git::commit" in seen["questions"]["target_tool"].criteria
    assert [entry["tool"] for entry in res["tools"]] == ["commit"]


def test_a_roster_with_no_tools_still_asks_the_server_question(monkeypatch, tmp_path):
    seen = install(monkeypatch, _server_probs(git=0.9, none=0.1))
    jev_engine.select_mcp_tools("commit", [_server("git")], str(tmp_path))
    assert set(seen["questions"]) == {"target_server", "is_relevant", "is_decisive"}


def test_an_oversized_roster_is_refused_before_any_work(tmp_path):
    huge = [_server(f"server-{i}", "x" * 4000, ["a", "b", "c", "d"]) for i in range(40)]
    with pytest.raises(JevValidationError) as excinfo:
        jev_engine.select_mcp_tools("commit", huge, str(tmp_path))
    assert "too long" in str(excinfo.value)


# 4. The verdicts
def test_answered_returns_the_winner_and_its_tools(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.7, github=0.2, none=0.1),
        _tool_probs(**{"git::git_commit": 0.6, "git::git_diff": 0.15, "github::create_pr": 0.05, "none": 0.2}),
    )
    res = jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert res["exists"] == "answered"
    assert res["matched"] is True
    assert res["primary"] == {"server": "git", "name": "git", "probability": 0.7}
    assert res["action"] == "auto"
    # The winning tool plus every sibling above the threshold; nothing from
    # another server, and nothing at 0.05.
    assert [entry["tool"] for entry in res["tools"]] == ["git_commit", "git_diff"]


def test_below_threshold_siblings_are_not_returned(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.9, none=0.1),
        _tool_probs(**{"git::git_commit": 0.85, "git::git_diff": 0.05, "none": 0.1}),
    )
    res = jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert [entry["tool"] for entry in res["tools"]] == ["git_commit"]


def test_every_tool_above_the_threshold_is_returned(monkeypatch, tmp_path):
    """The ceiling is 20; the threshold is what fills the list, and 30 clear it."""
    tools = [f"t{index}" for index in range(30)]
    tool_probs = _tool_probs(**{f"wide::t{index}": 0.15 for index in range(30)})
    tool_probs.update({"wide::t0": 0.30, "none": 0.0})
    install(monkeypatch, _server_probs(wide=0.9, none=0.1), tool_probs)
    res = jev_engine.select_mcp_tools("do the thing", [_server("wide", "many", tools)], str(tmp_path))
    assert len(res["tools"]) == 20
    assert "tools_truncated=30>max_tools=20" in res["reason_codes"]
    # Every returned tool belongs to the winning server and cleared the floor.
    assert all(entry["server"] == "wide" for entry in res["tools"])
    assert min(entry["probability"] for entry in res["tools"]) >= 0.12


def test_the_ceiling_on_the_tool_list_is_visible(monkeypatch, tmp_path):
    tools = [f"t{index}" for index in range(30)]
    tool_probs = _tool_probs(**{f"wide::t{index}": 0.15 for index in range(30)})
    tool_probs.update({"wide::t0": 0.30, "none": 0.0})
    install(monkeypatch, _server_probs(wide=0.9, none=0.1), tool_probs)
    res = jev_engine.select_mcp_tools("do the thing", [_server("wide", "many", tools)], str(tmp_path), max_tools=5)
    assert len(res["tools"]) == 5
    assert "tools_truncated=30>max_tools=5" in res["reason_codes"]
    assert res["action"] != "auto"


def test_the_default_tool_ceiling_is_twenty(monkeypatch, tmp_path):
    assert jev_engine.DEFAULT_MAX_MCP_TOOLS == 20
    tools = [f"t{index}" for index in range(30)]
    tool_probs = _tool_probs(**{f"wide::t{index}": 0.15 for index in range(30)})
    tool_probs.update({"wide::t0": 0.30, "none": 0.0})
    install(monkeypatch, _server_probs(wide=0.9, none=0.1), tool_probs)
    res = jev_engine.select_mcp_tools("do the thing", [_server("wide", "many", tools)], str(tmp_path))
    assert len(res["tools"]) == 20
    assert "tools_truncated=30>max_tools=20" in res["reason_codes"]


def test_a_tight_top_two_split_is_ambiguous(monkeypatch, tmp_path):
    """The harder case: a high-confidence Choice that picked arbitrarily."""
    install(
        monkeypatch,
        _server_probs(git=0.46, github=0.42, none=0.12),
        tool_probs=_tool_probs(**{"git::git_commit": 0.5, "github::create_pr": 0.5, "none": 0.0}),
        confidence=0.02,
    )
    res = jev_engine.select_mcp_tools("commit and open a pull request", ROSTER, str(tmp_path))
    assert res["exists"] == "ambiguous"
    assert res["matched"] is False
    assert res["primary"] is None
    assert res["tools"] == []
    assert [entry["server"] for entry in res["servers"]] == ["git", "github"]
    assert any(code.startswith("ambiguous_top2_gap=") for code in res["reason_codes"])
    assert res["action"] == "escalate"


def test_an_indecisive_noul_alone_is_enough_to_be_ambiguous(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.6, github=0.05, none=0.35),
        tool_probs=_tool_probs(**{"git::git_commit": 0.9, "none": 0.1}),
        decisive=0.1,
    )
    res = jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert res["exists"] == "ambiguous"
    assert res["matched"] is False
    assert "is_decisive=0.10<0.50" in res["reason_codes"]
    assert res["action"] == "review"


def test_ambiguous_keeps_the_runner_up_out_of_the_contenders(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.45, github=0.44, playwright=0.01, none=0.10),
        tool_probs=_tool_probs(**{"git::git_commit": 0.9, "none": 0.1}),
    )
    res = jev_engine.select_mcp_tools(
        "commit and open a pull request",
        ROSTER + [_server("playwright", "browser automation", ["click"])],
        str(tmp_path),
    )
    assert res["exists"] == "ambiguous"
    assert [entry["server"] for entry in res["servers"]] == ["git", "github"]


def test_absent_when_nothing_fits(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.2, github=0.1, none=0.7),
        tool_probs=_tool_probs(none=1.0),
        relevance=0.1,
    )
    res = jev_engine.select_mcp_tools("fix a typo in the readme", ROSTER, str(tmp_path))
    assert res["exists"] == "absent"
    assert res["matched"] is False
    assert res["tools"] == []
    assert res["primary"] is None
    assert "relevance_prob=0.10<0.50" in res["reason_codes"]


def test_partial_when_the_choice_wins_among_servers_that_do_not_fit(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.6, github=0.3, none=0.1),
        tool_probs=_tool_probs(**{"git::git_commit": 0.9, "none": 0.1}),
        relevance=0.2,
    )
    res = jev_engine.select_mcp_tools("fix a typo in the readme", ROSTER, str(tmp_path))
    assert res["exists"] == "partial"
    assert res["matched"] is False
    assert res["primary"] is None
    assert res["tools"] == []
    # A confident Choice cannot overrule the presence Noul, and the code says why.
    assert "relevance_prob=0.20<0.50" in res["reason_codes"]


def test_the_tool_choice_never_overrides_the_server_choice(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.8, github=0.15, none=0.05),
        tool_probs=_tool_probs(**{"github::create_pr": 0.9, "git::git_commit": 0.15, "none": 0.0}),
    )
    res = jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert res["primary"]["server"] == "git"
    # The speculative tool question can land on another server's tool; that
    # winner is dropped, and only the winning server's tools are reported.
    assert [entry["tool"] for entry in res["tools"]] == ["git_commit"]
    assert all(entry["server"] == res["primary"]["server"] for entry in res["tools"])


def test_a_tool_winner_from_another_server_does_not_suppress_its_siblings(monkeypatch, tmp_path):
    install(
        monkeypatch,
        _server_probs(git=0.8, github=0.15, none=0.05),
        tool_probs=_tool_probs(**{"github::create_pr": 0.9, "git::git_commit": 0.02, "none": 0.0}),
    )
    res = jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert res["primary"]["server"] == "git"
    assert res["tools"] == []


# 5. Bounds and cost
def test_more_tool_options_than_the_choice_ceiling_are_reported(monkeypatch, tmp_path):
    """A 300-tool roster cannot all be offered; what was dropped is named."""
    servers = [_server(f"s{index}", f"server {index}", [f"t{n}" for n in range(10)]) for index in range(31)]
    seen = install(monkeypatch, _server_probs(**{f"s{index}": 0.03 for index in range(31)}))
    res = jev_engine.select_mcp_tools("do the thing", servers, str(tmp_path))
    assert len(seen["questions"]["target_tool"].criteria) <= 251   # 250 + `none`
    assert res["candidates_truncated"] is True
    assert res["action"] != "auto"
    assert any(code.startswith("tools_offered_truncated=") for code in res["reason_codes"])


def test_more_servers_than_the_server_ceiling_are_reported(monkeypatch, tmp_path):
    servers = [_server(f"s{index}", f"server {index}") for index in range(70)]
    seen = install(monkeypatch, _server_probs(**{f"s{index}": 0.01 for index in range(64)}))
    res = jev_engine.select_mcp_tools("do the thing", servers, str(tmp_path))
    assert len(seen["questions"]["target_server"].criteria) == 65   # 64 + `none`
    assert res["candidates_truncated"] is True
    assert "servers_truncated" in res["reason_codes"]
    assert any(entry["reason"] == "over_limit" for entry in res["excluded"])


def test_truncated_context_never_auto_accepts(monkeypatch, tmp_path):
    install(monkeypatch, _server_probs(git=0.9, none=0.1), truncated=True)
    res = jev_engine.select_mcp_tools("commit", ROSTER, str(tmp_path))
    assert res["action"] == "review"
    assert "context_truncated" in res["reason_codes"]


@pytest.mark.parametrize("kwargs", [{"max_tools": 0}, {"max_tools": 500}, {"max_servers": -1}, {"max_servers": 99}])
def test_an_out_of_range_ceiling_is_refused(tmp_path, kwargs):
    with pytest.raises(JevValidationError):
        jev_engine.select_mcp_tools("commit", ROSTER, str(tmp_path), **kwargs)


def test_a_non_integer_ceiling_is_refused(tmp_path):
    with pytest.raises(JevValidationError):
        jev_engine.select_mcp_tools("commit", ROSTER, str(tmp_path), max_tools=True)


def test_one_round_trip_per_call(monkeypatch, tmp_path):
    calls = []
    original = jev_engine.execute_system_one

    def _counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    monkeypatch.setattr(jev_engine, "execute_system_one", _counting)
    jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert len(calls) == 1


# 6. Task text
def test_a_roster_with_no_tools_anywhere_runs_through_validation(monkeypatch, tmp_path):
    """No `target_tool` question, and the response still has to validate.

    The questions and the answers have to match exactly, so dropping a question
    is a real branch and not a formality.
    """
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    res = jev_engine.select_mcp_tools(
        "commit the staged changes",
        [_server("git", "read and write local git repositories, commits and branches")],
        str(tmp_path),
    )
    assert res["exists"] in {"absent", "partial", "answered", "ambiguous"}
    assert res["tools"] == []
    assert res["usage"]["input_tokens"] is not None


def test_a_task_file_is_judged_alongside_the_task(monkeypatch, tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# Phase 2\nOpen a pull request for the finished branch.\n", encoding="utf-8")
    seen = install(monkeypatch, _server_probs(github=0.9, none=0.1))
    jev_engine.select_mcp_tools("do phase 2", ROSTER, str(tmp_path), str(plan))
    assert "phase 2" in seen["state"]["task"].lower()
    assert "pull request" in seen["state"]["task"].lower()


def test_a_server_name_containing_the_key_separator_still_round_trips(monkeypatch, tmp_path):
    """The option key is `server::tool`; a caller may put anything in a name."""
    seen = install(
        monkeypatch,
        _server_probs(**{"ns::weird": 0.9, "none": 0.1}),
        _tool_probs(**{"ns::weird::do_it": 0.9, "none": 0.1}),
    )
    res = jev_engine.select_mcp_tools(
        "do the thing",
        [{"name": "ns::weird", "tools": [{"name": "do_it", "description": "do the thing"}]}],
        str(tmp_path),
    )
    assert "ns::weird::do_it" in seen["questions"]["target_tool"].criteria
    assert res["primary"]["server"] == "ns::weird"
    assert [(e["server"], e["tool"]) for e in res["tools"]] == [("ns::weird", "do_it")]


def test_a_tie_below_the_contender_floor_still_names_the_top_two(monkeypatch, tmp_path):
    """A flat split in a six-server roster clears no floor, and must not read
    as an empty verdict."""
    probs = {f"s{index}": 0.05 for index in range(5)}
    probs["none"] = 0.0
    install(monkeypatch, _server_probs(**probs), _tool_probs(none=1.0), relevance=0.9, decisive=0.2)
    res = jev_engine.select_mcp_tools(
        "do the thing", [_server(f"s{index}", f"server {index}") for index in range(5)], str(tmp_path)
    )
    assert res["exists"] == "ambiguous"
    assert len(res["servers"]) == 2
    assert "comparably usable" in res["summary"]


def test_a_task_file_outside_the_allowlist_is_refused(monkeypatch, tmp_path):
    with pytest.raises(JevValidationError):
        jev_engine.select_mcp_tools("do phase 2", ROSTER, str(tmp_path), "C:/Windows/System32/drivers/etc/hosts")


def test_the_state_carries_the_task_and_the_exclusions(monkeypatch, tmp_path):
    seen = install(monkeypatch, _server_probs(git=0.9, none=0.1))
    jev_engine.select_mcp_tools("commit the staged changes", ROSTER, str(tmp_path))
    assert seen["state"]["task"] == "commit the staged changes"
    assert seen["state"]["excluded_servers"] == ["jev-engine"]
    assert seen["state"]["servers_evaluated"] == 2
    assert seen["state"]["tools_evaluated"] == 3
