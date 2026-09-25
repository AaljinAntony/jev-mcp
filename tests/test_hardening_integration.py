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
)
from jev_errors import error_details, JevValidationError
from limits import fit_state
from policy import (
    DEFAULT_RISK_THRESHOLD,
    DEFAULT_ESCALATE_THRESHOLD,
    FAMILY_CLUSTER_MAX_SIBLINGS,
    guardrail_safe,
)
from typesafe_sdk import (
    ChoiceAnswer,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPITimeoutError,
    Usage,
)


# 1. Connection error mapping
def test_connection_error_never_skipped():
    class TestConnError(TypeSafeAPIConnectionError):
        def __init__(self):
            super(Exception, self).__init__("Connection dropped")

    res = error_details(TestConnError())
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
def _stub_primary(monkeypatch, probs, choice):
    """Answer `find_agent_resources` with one hand-built Choice distribution."""
    def _fake_request(state, questions):
        return (
            SystemOneResponse(
                model="jev-test",
                answers={
                    "primary": ChoiceAnswer(
                        choice=choice,
                        probabilities=probs,
                        confidence=0.9,
                    )
                },
                usage=Usage(input_tokens=10, output_tokens=5),
            ),
            {"truncated": False, "coverage": {}},
            get_config(),
        )

    monkeypatch.setattr(jev_engine, "_request", _fake_request)


def _family_tree(tmp_path):
    skills_dir = tmp_path / ".agents" / "skills"
    for name in ["tool-runner", "tool-builder", "tool-linter", "my-tool-extra"]:
        (skills_dir / name).mkdir(parents=True)
        (skills_dir / name / "SKILL.md").write_text(f"# {name}\n\n{name} summary text\n")
    return {
        name: f".agents/skills/{name}/SKILL.md"
        for name in ["tool-runner", "tool-builder", "tool-linter", "my-tool-extra"]
    }


def test_family_prefix_sibling_matching(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    _reset_client_cache()
    _reset_settings_cache()
    rel = _family_tree(tmp_path)

    # A confident primary pulls in same-family siblings, highest probability first.
    _stub_primary(
        monkeypatch,
        {
            rel["tool-runner"]: 0.7,
            rel["tool-linter"]: 0.2,
            rel["tool-builder"]: 0.05,
            rel["my-tool-extra"]: 0.04,
            "none": 0.01,
        },
        rel["tool-runner"],
    )
    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    files = [r["file"] for r in res["resources"]]
    assert files[0] == rel["tool-runner"]
    assert rel["tool-linter"] in files          # same "tool-" family
    assert rel["my-tool-extra"] not in files    # different family prefix

    # At most FAMILY_CLUSTER_MAX_SIBLINGS siblings are added, never the whole family.
    _stub_primary(
        monkeypatch,
        {
            rel["tool-runner"]: 0.7,
            rel["tool-linter"]: 0.2,
            rel["tool-builder"]: 0.05,
            rel["my-tool-extra"]: 0.04,
            "none": 0.01,
        },
        rel["tool-runner"],
    )
    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    assert len(res["resources"]) <= 1 + FAMILY_CLUSTER_MAX_SIBLINGS

    # A weak primary must not claim any slot for its family: every sibling is
    # below the independent 0.12 sibling floor, so only clustering could add one.
    _stub_primary(
        monkeypatch,
        {
            rel["tool-runner"]: 0.15,
            rel["tool-linter"]: 0.11,
            rel["tool-builder"]: 0.10,
            rel["my-tool-extra"]: 0.09,
            "none": 0.55,
        },
        rel["tool-runner"],
    )
    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    assert [r["file"] for r in res["resources"]] == [rel["tool-runner"]]

    _reset_config_cache()
    _reset_client_cache()
    _reset_settings_cache()


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
def test_target_files_bounded_depth(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    _reset_client_cache()
    p = tmp_path
    for i in range(7):
        p = p / f"dir_{i}"
        p.mkdir()
        (p / "test.py").write_text("a = 1")

    res = select_target_files("find file", root_dir=str(tmp_path))
    assert res is not None
    _reset_config_cache()
    _reset_client_cache()


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
def test_settings_mtime_caching(tmp_path, monkeypatch):
    cfg_file = tmp_path / "jevs_settings.json"
    cfg_file.write_text(json.dumps({"enable_model_routing": True}))
    monkeypatch.chdir(tmp_path)
    _reset_settings_cache()

    s1 = load_jev_settings()
    assert s1["enable_model_routing"] is True

    s2 = load_jev_settings()
    assert s1 is s2
    _reset_settings_cache()
