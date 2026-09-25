"""Table-driven tests verifying identical envelope keys across all branches of every tool."""

import json
from pathlib import Path
import pytest

from typesafe_sdk import ChoiceAnswer, SystemOneResponse, Usage
import jev_engine


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


class TestFindAgentResourcesEnvelopeKeys:
    def test_keys_identical_across_branches(self, tmp_path, monkeypatch):
        # 1. No candidates branch (empty directory)
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        res_no_candidates = jev_engine.find_agent_resources("task", str(empty_dir))

        # Setup directory with skills
        skills_dir = tmp_path / "with_skills"
        s1 = skills_dir / ".agents" / "skills" / "s1"
        s1.mkdir(parents=True)
        f1 = s1 / "SKILL.md"
        f1.write_text("# Skill 1", encoding="utf-8")
        rel1 = f1.relative_to(skills_dir).as_posix()

        # 2. Match branch
        res_match = jev_engine.find_agent_resources("Skill 1", str(skills_dir))

        # 3. No match branch (force mock response to pick "none" or nonexistent)
        fitted_mock = {
            "truncated": False,
            "coverage": {
                "complete": True,
                "original_chars": 50,
                "evaluated_chars": 50,
                "estimated_tokens": {"state": 10, "questions": 5, "longest_question": 5},
                "estimator": "chars/4",
            },
        }
        cfg_mock = jev_engine.get_config()
        no_match_response = SystemOneResponse(
            model="jev-mock",
            answers={
                "primary": ChoiceAnswer(choice="none", probabilities={rel1: 0.05, "none": 0.95}, confidence=0.9),
                "secondary": ChoiceAnswer(choice="none", probabilities={rel1: 0.0, "none": 1.0}, confidence=1.0),
            },
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        monkeypatch.setattr(jev_engine, "_request", lambda s, q: (no_match_response, fitted_mock, cfg_mock))
        res_no_match = jev_engine.find_agent_resources("unknown", str(skills_dir))

        keys_no_candidates = set(res_no_candidates.keys())
        keys_match = set(res_match.keys())
        keys_no_match = set(res_no_match.keys())

        assert keys_no_candidates == keys_match, f"Diff: {keys_no_candidates ^ keys_match}"
        assert keys_match == keys_no_match, f"Diff: {keys_match ^ keys_no_match}"
        assert res_no_candidates["coverage"] is not None
        assert res_no_candidates["action"] == "auto"
        assert res_no_candidates["confidence"] is None


class TestSelectTargetFilesEnvelopeKeys:
    def test_keys_identical_across_branches(self, tmp_path, monkeypatch):
        # 1. No candidates branch (empty directory, git disabled)
        empty_dir = tmp_path / "empty_dir"
        empty_dir.mkdir()
        monkeypatch.setattr(jev_engine, "_discover_files_git", lambda *a, **k: None)
        res_no_candidates = jev_engine.select_target_files("find file", str(empty_dir))
        assert res_no_candidates["exists"] == "no_candidates"

        # Setup files in workspace
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "main.py").write_text("print('hello')", encoding="utf-8")
        (ws / "util.py").write_text("def helper(): pass", encoding="utf-8")

        # 2. Match branch
        res_match = jev_engine.select_target_files("find main", str(ws))

        # 3. Escape-hatch branch (model selects "none")
        fitted_mock = {
            "truncated": False,
            "coverage": {
                "complete": True,
                "original_chars": 50,
                "evaluated_chars": 50,
                "estimated_tokens": {"state": 10, "questions": 5, "longest_question": 5},
                "estimator": "chars/4",
            },
        }
        cfg_mock = jev_engine.get_config()
        escape_response = SystemOneResponse(
            model="jev-mock",
            answers={
                "target_file": ChoiceAnswer(choice="none", probabilities={"main.py": 0.1, "util.py": 0.1, "none": 0.8}, confidence=0.7),
            },
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        monkeypatch.setattr(jev_engine, "_request", lambda s, q: (escape_response, fitted_mock, cfg_mock))
        res_escape = jev_engine.select_target_files("find other", str(ws))

        keys_no_candidates = set(res_no_candidates.keys())
        keys_match = set(res_match.keys())
        keys_escape = set(res_escape.keys())

        assert keys_no_candidates == keys_match, f"Diff: {keys_no_candidates ^ keys_match}"
        assert keys_match == keys_escape, f"Diff: {keys_match ^ keys_escape}"
        assert res_no_candidates["coverage"] is not None
        assert res_no_candidates["action"] == "auto"
        assert res_no_candidates["confidence"] is None


class TestSelectModelTierEnvelopeKeys:
    def test_keys_identical_across_branches(self, monkeypatch):
        # 1. Routing off branch
        monkeypatch.setattr(
            jev_engine,
            "load_jev_settings",
            lambda: {
                "enable_model_routing": False,
                "models": {"fast": "f", "balanced": "b", "frontier": "fr"},
                "source": "mock",
            },
        )
        res_disabled = jev_engine.select_model_tier("simple task")

        # 2. Routing on, normal confidence
        monkeypatch.setattr(
            jev_engine,
            "load_jev_settings",
            lambda: {
                "enable_model_routing": True,
                "models": {"fast": "f", "balanced": "b", "frontier": "fr"},
                "source": "mock",
            },
        )
        res_on = jev_engine.select_model_tier("fix typo")

        # 3. Routing on, low confidence (escalates)
        fitted_mock = {
            "truncated": False,
            "coverage": {
                "complete": True,
                "original_chars": 50,
                "evaluated_chars": 50,
                "estimated_tokens": {"state": 10, "questions": 5, "longest_question": 5},
                "estimator": "chars/4",
            },
        }
        cfg_mock = jev_engine.get_config()
        low_conf_response = SystemOneResponse(
            model="jev-mock",
            answers={
                "tier": ChoiceAnswer(
                    choice="balanced",
                    probabilities={"fast": 0.33, "balanced": 0.34, "frontier": 0.33},
                    confidence=0.1,
                ),
            },
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        monkeypatch.setattr(jev_engine, "_request", lambda s, q: (low_conf_response, fitted_mock, cfg_mock))
        res_low_conf = jev_engine.select_model_tier("ambiguous task")

        keys_disabled = set(res_disabled.keys())
        keys_on = set(res_on.keys())
        keys_low_conf = set(res_low_conf.keys())

        assert keys_disabled == keys_on, f"Diff: {keys_disabled ^ keys_on}"
        assert keys_on == keys_low_conf, f"Diff: {keys_on ^ keys_low_conf}"
        assert res_disabled["coverage"] is not None
        assert res_low_conf["action"] == "escalate"
