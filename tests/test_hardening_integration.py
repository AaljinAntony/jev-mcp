"""Integration and regression test suite for Jev-TypeSafe MCP hardening."""

import json
import os
import threading
import time
from pathlib import Path
import pytest

from config import get_config, _reset_config_cache
import jev_engine
from jev_engine import (
    get_client,
    _reset_client_cache,
    load_jev_settings,
    _reset_settings_cache,
    _validate_root_dir,
    select_target_files,
    find_agent_resources,
    MAX_WALK_DEPTH,
)
from jev_errors import error_details, JevValidationError
from limits import fit_state, MAX_CHOICE_OPTIONS
from policy import (
    DEFAULT_RISK_THRESHOLD,
    DEFAULT_ESCALATE_THRESHOLD,
    FAMILY_CLUSTER_MAX_SIBLINGS,
    FAMILY_CLUSTER_MIN_PROB,
    guardrail_safe,
)
from typesafe_sdk import TypeSafeAPIConnectionError


# 1. Connection error mapping
def test_connection_error_never_skipped():
    # Constructed through the SDK's own __init__ (message, request_id, body), so
    # this exercises the real class rather than a hand-rolled Exception that
    # happens to inherit the same name.
    err = TypeSafeAPIConnectionError("Connection dropped")
    res = error_details(err)
    assert res["code"] == "API_ERROR"
    assert res["retryable"] is True
    assert "Could not connect to TypeSafe" in res["message"]


# 2. Shared risk constants
def test_risk_constants_and_guardrail_boundaries():
    assert DEFAULT_RISK_THRESHOLD == 0.20
    assert DEFAULT_ESCALATE_THRESHOLD == 0.50
    assert guardrail_safe("auto", destructive_prob=0.19, git_modify_prob=0.19) is True
    assert guardrail_safe("auto", destructive_prob=0.20, git_modify_prob=0.0) is False
    assert guardrail_safe("auto", destructive_prob=0.0, git_modify_prob=0.20) is False


# 3. Sibling family-prefix matching
def _family_tree(tmp_path):
    skills_dir = tmp_path / ".agents" / "skills"
    for name in ["tool-runner", "tool-builder", "tool-linter", "my-tool-extra"]:
        (skills_dir / name).mkdir(parents=True)
        (skills_dir / name / "SKILL.md").write_text(f"# {name}\n\n{name} summary text\n")
    return {
        name: f".agents/skills/{name}/SKILL.md"
        for name in ["tool-runner", "tool-builder", "tool-linter", "my-tool-extra"]
    }


def test_family_cluster_adds_siblings_for_confident_primary(tmp_path, monkeypatch, stub_choice):
    """A high-probability primary pulls in same-family siblings, newest first."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    rel = _family_tree(tmp_path)

    # tool-builder and tool-linter share the primary's "tool-" family; every
    # sibling sits below the independent 0.12 floor, so only clustering can add
    # them. my-tool-extra does not start with "tool-" and must never be added.
    stub_choice(
        rel["tool-runner"],
        {
            rel["tool-runner"]: 0.70,
            rel["tool-linter"]: 0.11,
            rel["tool-builder"]: 0.10,
            rel["my-tool-extra"]: 0.08,
            "none": 0.01,
        },
    )
    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    files = [r["file"] for r in res["resources"]]

    assert files[0] == rel["tool-runner"]
    assert set(files[1:]) == {rel["tool-linter"], rel["tool-builder"]}
    assert rel["my-tool-extra"] not in files


def test_family_cluster_never_claims_every_slot(tmp_path, monkeypatch, stub_choice):
    """Clustering is capped at FAMILY_CLUSTER_MAX_SIBLINGS, family size be damned."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    rel = _family_tree(tmp_path)
    assert FAMILY_CLUSTER_MAX_SIBLINGS < 3, "fixture no longer exercises the cap"

    stub_choice(
        rel["tool-runner"],
        {
            rel["tool-runner"]: 0.70,
            rel["tool-linter"]: 0.11,
            rel["tool-builder"]: 0.10,
            rel["my-tool-extra"]: 0.08,
            "none": 0.01,
        },
    )
    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    assert len(res["resources"]) == 1 + FAMILY_CLUSTER_MAX_SIBLINGS


def test_family_cluster_suppressed_for_weak_primary(tmp_path, monkeypatch, stub_choice):
    """A low-probability primary must not claim the sibling slots."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    rel = _family_tree(tmp_path)

    # Below FAMILY_CLUSTER_MIN_PROB: this is a guess, not a decision, so it is
    # not allowed to fill the result with its own directory family.
    probs = {
        rel["tool-runner"]: 0.15,
        rel["tool-linter"]: 0.11,
        rel["tool-builder"]: 0.10,
        rel["my-tool-extra"]: 0.09,
        "none": 0.55,
    }
    assert probs[rel["tool-runner"]] < FAMILY_CLUSTER_MIN_PROB
    stub_choice(rel["tool-runner"], probs)
    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    assert [r["file"] for r in res["resources"]] == [rel["tool-runner"]]


# 4. Thread-safe client caching under concurrent access
def test_concurrent_get_client_thread_safety(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123456789012345678901234567890")
    _reset_config_cache()
    _reset_client_cache()

    clients = []
    errors = []

    def worker():
        try:
            client = get_client()
            clients.append(client)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(clients) == 10
    first = clients[0]
    for c in clients:
        assert c is first

    # Close the pool this test opened. `_reset_client_cache` only drops the
    # reference; without an explicit close, every run of this test leaked one
    # connection pool.
    try:
        first.close()
    finally:
        _reset_client_cache()
        _reset_config_cache()


# 5. Settings parse error handling
def test_settings_parse_error_logging(tmp_path, monkeypatch):
    bad_json = tmp_path / "jevs_settings.json"
    bad_json.write_text("{ unquoted_key: invalid }")
    monkeypatch.chdir(tmp_path)
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["enable_model_routing"] is False
    _reset_settings_cache()


# 6. Windows drive root rejection
def test_validate_root_dir_security():
    for drive in ["C:\\", "D:\\", "E:\\", "Z:\\"]:
        with pytest.raises(JevValidationError):
            _validate_root_dir(drive)


# 7. Depth-bounded directory walk
def test_target_files_respects_walk_depth(tmp_path, monkeypatch):
    """Files past MAX_WALK_DEPTH are never offered to the model."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    _reset_client_cache()

    # One file at each depth 0..MAX_WALK_DEPTH+1. `_prune_dirnames` clears a
    # directory's children once the directory itself is MAX_WALK_DEPTH levels
    # down, so depth MAX_WALK_DEPTH-1 is the last one that yields a file.
    deepest = tmp_path
    for i in range(MAX_WALK_DEPTH + 2):
        level = deepest / f"d{i}"
        level.mkdir()
        (level / f"f{i}.py").write_text("x = 1")
        deepest = level
    (tmp_path / "top.py").write_text("x = 1")

    seen = {}
    real_request = jev_engine._request

    def _spy(state, questions):
        seen["criteria"] = set(questions["target_file"].criteria)
        return real_request(state, questions)

    monkeypatch.setattr(jev_engine, "_request", _spy)
    res = select_target_files("find the top file", root_dir=str(tmp_path))
    criteria = seen["criteria"]

    assert "top.py" in criteria
    # `_prune_dirnames` clears a directory's children once the directory itself
    # sits MAX_WALK_DEPTH levels down, so the deepest offered file is the one
    # in the directory one level above that.
    depths = [len(Path(c).parts) - 1 for c in criteria if c != "none"]
    assert max(depths) == MAX_WALK_DEPTH - 1
    assert f"d{MAX_WALK_DEPTH}/" not in "".join(criteria), "depth bound not enforced"
    assert not any(f"d{MAX_WALK_DEPTH + 1}/" in c for c in criteria)
    assert res["candidates_truncated"] is False


def test_target_files_reports_candidate_overflow(tmp_path, monkeypatch):
    """Past the discovery cap the tool reports truncation and never auto-accepts."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    _reset_client_cache()

    for i in range(MAX_CHOICE_OPTIONS + 1):
        (tmp_path / f"f{i:04d}.py").write_text("x = 1")

    seen = {}
    real_request = jev_engine._request

    def _spy(state, questions):
        seen["state"] = state
        return real_request(state, questions)

    monkeypatch.setattr(jev_engine, "_request", _spy)
    res = select_target_files("find a file", root_dir=str(tmp_path))

    # One file past the cap was never offered, so the winner cannot be defended
    # from the evidence the model actually saw.
    assert res["candidates_truncated"] is True
    assert res["action"] != "auto"
    assert seen["state"]["candidates_evaluated"] == MAX_CHOICE_OPTIONS
    fields = res["coverage"]["candidate_fields"]
    assert fields["complete"] is False
    assert fields["candidates_considered"] == MAX_CHOICE_OPTIONS


# 8. fit_state token reuse & defensive copy
def test_fit_state_defensive_copy():
    state = {"hello": "world"}
    questions = {"q1": "test question"}
    fitted = fit_state(state, questions)
    assert fitted["truncated"] is False
    # Mutating returned state should not affect original
    fitted["state"]["mutated"] = True
    assert "mutated" not in state


# 9. Config caching and reset
def test_config_caching(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    c1 = get_config()
    assert c1.mock is True

    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    c2 = get_config()
    assert c2.mock is True

    _reset_config_cache()
    c3 = get_config()
    assert c3.mock is False
    _reset_config_cache()


# 10. Settings mtime caching
def test_settings_cache_invalidates_on_mtime_change(tmp_path, monkeypatch):
    cfg_file = tmp_path / "jevs_settings.json"
    cfg_file.write_text(json.dumps({"enable_model_routing": False}))
    monkeypatch.chdir(tmp_path)
    _reset_settings_cache()

    assert load_jev_settings()["enable_model_routing"] is False

    cfg_file.write_text(json.dumps({"enable_model_routing": True}))
    # Filesystem mtime granularity is coarse (and tmp_path is often a FAT-like
    # volume on CI); a same-second rewrite can leave st_mtime unchanged, which
    # would make the test flaky rather than wrong. Force the observable change.
    bump = time.time() + 2
    os.utime(cfg_file, (bump, bump))

    assert load_jev_settings()["enable_model_routing"] is True
    _reset_settings_cache()


def test_settings_cache_serves_a_copy_not_the_cache_object(tmp_path, monkeypatch):
    """Identity would test the implementation; equality plus non-identity tests
    the behaviour that actually matters — a caller cannot mutate module state."""
    cfg_file = tmp_path / "jevs_settings.json"
    cfg_file.write_text(json.dumps({"enable_model_routing": True}))
    monkeypatch.chdir(tmp_path)
    _reset_settings_cache()

    s1 = load_jev_settings()
    s2 = load_jev_settings()
    assert s1 == s2
    assert s1 is not s2

    s1["scan_paths"].append("injected")
    s1["enable_model_routing"] = False
    s3 = load_jev_settings()
    assert "injected" not in s3["scan_paths"]
    assert s3["enable_model_routing"] is True
    _reset_settings_cache()

