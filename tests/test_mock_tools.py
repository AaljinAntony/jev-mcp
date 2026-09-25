"""Offline mock-mode tool tests (no TYPESAFE_API_KEY required)."""

import json
import os
from pathlib import Path
import pytest

from jev_errors import JevValidationError
from typesafe_sdk import (
    ChoiceAnswer,
    NoulAnswer,
    SystemOneResponse,
    Usage,
)

import jev_engine
import jev_mcp


def _fake_response(answers, model="jev-test"):
    return SystemOneResponse(model=model, answers=answers, usage=Usage(input_tokens=12, output_tokens=6))


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


class TestGuardrailTool:
    def test_mock_happy_path_all_keys(self):
        result = jev_engine.verify_command("git status")
        # Backward-compatible keys
        assert "safe" in result
        assert "destructive_prob" in result
        assert "git_modify_prob" in result
        # New envelope keys
        assert result["action"] in {"auto", "review", "escalate"}
        assert result["confidence"] >= 0
        assert isinstance(result["reason_codes"], list)
        assert "model" in result and result["model"].endswith("+mock")
        assert "usage" in result and "input_tokens" in result["usage"]
        assert "truncated" in result and result["truncated"] is False

    def test_safe_command_can_be_auto(self):
        result = jev_engine.verify_command("git status")
        assert result["safe"] is True
        assert result["action"] == "auto"

    def test_destructive_command_never_safe(self):
        result = jev_engine.verify_command("rm -rf /tmp/scratch")
        assert result["safe"] is False
        assert result["action"] in {"review", "escalate"}

    def test_uniform_judgment_escalates_never_safe(self, monkeypatch):
        # AC: a weak/ambiguous Jev answer must yield action != auto and safe == False.
        monkeypatch.setattr(
            jev_engine,
            "execute_system_one",
            lambda *a, **k: _fake_response(
                {
                    "is_destructive": NoulAnswer(noul=0.5),
                    "modifies_git": NoulAnswer(noul=0.5),
                }
            ),
        )
        result = jev_engine.verify_command("something ambiguous")
        assert result["action"] == "escalate"
        assert result["safe"] is False

    def test_malformed_response_returns_error_envelope_not_safe(self, monkeypatch):
        # Invalid responses never read as safe:true.
        monkeypatch.setattr(
            jev_engine,
            "execute_system_one",
            lambda *a, **k: _fake_response({"is_destructive": NoulAnswer(noul=0.1)}),
        )
        envelope = jev_mcp._run("guardrail_command", lambda: jev_engine.verify_command("x"))
        assert "error" in envelope
        assert envelope["error"]["code"] == "INVALID_RESPONSE"
        assert "safe" not in envelope

    def test_tool_returns_json_serializable(self):
        result = jev_engine.verify_command("git status")
        json.dumps(result)  # must not raise


class TestSearchAgentSkills:
    def test_mock_offline_with_files(self, tmp_path, monkeypatch):
        skills = tmp_path / ".agents" / "skills"
        (skills / "tool-x").mkdir(parents=True)
        (skills / "tool-y").mkdir(parents=True)
        (skills / "tool-x" / "SKILL.md").write_text("# tool-x\ntheme and layout helpers", encoding="utf-8")
        (skills / "tool-y" / "SKILL.md").write_text("# tool-y\nui dialog navigation", encoding="utf-8")

        result = jev_engine.find_agent_resources("theme layout helper", str(tmp_path))
        assert "matched" in result and isinstance(result["matched"], bool)
        assert "count" in result
        assert "primary" in result
        assert "ranked" in result and isinstance(result["ranked"], list)
        assert result["ranked"][0]["file"] == result["file"]
        assert "action" in result and result["action"] in {"auto", "review", "escalate"}
        assert "confidence" in result
        assert "model" in result and result["model"].endswith("+mock")
        assert "usage" in result

    def test_mock_with_no_skills(self, tmp_path):
        result = jev_engine.find_agent_resources("anything", str(tmp_path))
        assert result["matched"] is False
        assert result["count"] == 0
        assert result["resources"] == []

    def test_mock_search_outside_root_skipped(self, tmp_path, monkeypatch):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "test.md").write_text("# Outside\nnot in root", encoding="utf-8")
        root = tmp_path / "root"
        root.mkdir()
        monkeypatch.setattr(jev_engine, "get_scan_paths", lambda r: [outside])
        result = jev_engine.find_agent_resources("test", str(root))
        assert result["matched"] is False
        assert result["count"] == 0
        assert result["resources"] == []


class TestSearchTargetFiles:
    def test_mock_offline_picks_file(self, tmp_path):
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "core.py").write_text("def run(): pass", encoding="utf-8")
        result = jev_engine.select_target_files("implement core", str(tmp_path))
        assert result["matched"] is True
        assert len(result["files"]) == 1
        assert result["exists"] == "answered"
        assert result["files"][0] == result["ranked"][0]["file"]
        assert 0 <= result["probability"] <= 1
        assert "action" in result and "confidence" in result
        assert "model" in result and "usage" in result

    def test_none_escape_matched_false(self, tmp_path, monkeypatch):
        target = tmp_path / "src" / "core.py"
        target.parent.mkdir(parents=True)
        target.write_text("def run(): pass", encoding="utf-8")

        monkeypatch.setattr(
            jev_engine,
            "execute_system_one",
            lambda *a, **k: _fake_response(
                {
                    "target_file": ChoiceAnswer(
                        choice="none",
                        confidence=0.8,
                        probabilities={"src/core.py": 0.2, "none": 0.8},
                    )
                }
            ),
        )
        result = jev_engine.select_target_files("buy groceries", str(tmp_path))
        assert result["matched"] is False
        assert result["files"] == []
        assert result["exists"] in {"absent", "partial"}


class TestSelectModelTier:
    def test_routing_off_disabled_payload(self):
        result = jev_engine.select_model_tier("typo fix")
        assert result["enabled"] is False
        assert result["recommended_tier"] is None
        assert result["action"] == "review"

    def test_routing_on_returns_tier_and_meta(self, monkeypatch):
        monkeypatch.setattr(
            jev_engine,
            "load_jev_settings",
            lambda: {
                "enable_model_routing": True,
                "models": {"fast": "p/f", "balanced": "p/b", "frontier": "p/x"},
                "scan_paths": [],
                "source": "fake",
            },
        )
        result = jev_engine.select_model_tier("fix a typo in the README")
        assert result["enabled"] is True
        assert result["recommended_tier"] in {"fast", "balanced", "frontier"}
        assert result["recommended_model"]
        assert result["action"] in {"auto", "review", "escalate"}
        assert result["confidence"] is not None
        assert result["model"].endswith("+mock")
        assert result["usage"] is not None

    def test_low_confidence_escalates_keeps_tier(self, monkeypatch):
        monkeypatch.setattr(
            jev_engine,
            "load_jev_settings",
            lambda: {
                "enable_model_routing": True,
                "models": {"fast": "p/f", "balanced": "p/b", "frontier": "p/x"},
                "scan_paths": [],
                "source": "fake",
            },
        )
        monkeypatch.setattr(
            jev_engine,
            "execute_system_one",
            lambda *a, **k: _fake_response(
                {"tier": ChoiceAnswer(choice="balanced", confidence=0.2, probabilities={"fast": 0.33, "balanced": 0.34, "frontier": 0.33})}
            ),
        )
        result = jev_engine.select_model_tier("design a protocol")
        assert result["action"] == "escalate"
        assert result["recommended_tier"] == "balanced"


class TestInputLengthGuardrails:
    def test_verify_command_oversized_rejected(self):
        huge = "a" * (jev_engine.MAX_INPUT_CHARS + 1)
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine.verify_command(huge)
        assert "command" in str(exc_info.value)
        assert "too long" in str(exc_info.value)

    def test_find_agent_resources_oversized_task_rejected(self, tmp_path):
        huge = "a" * (jev_engine.MAX_INPUT_CHARS + 1)
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine.find_agent_resources(huge, str(tmp_path))
        assert "task" in str(exc_info.value)

    def test_find_agent_resources_oversized_root_dir_rejected(self):
        huge = "a" * (jev_engine.MAX_INPUT_CHARS + 1)
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine.find_agent_resources("task", huge)
        assert "root_dir" in str(exc_info.value)

    def test_select_target_files_oversized_task_rejected(self, tmp_path):
        huge = "a" * (jev_engine.MAX_INPUT_CHARS + 1)
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine.select_target_files(huge, str(tmp_path))
        assert "task" in str(exc_info.value)

    def test_select_model_tier_oversized_task_rejected(self):
        huge = "a" * (jev_engine.MAX_INPUT_CHARS + 1)
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine.select_model_tier(huge)
        assert "task" in str(exc_info.value)

    def test_mcp_tool_oversized_returns_error_envelope(self):
        huge = "a" * (jev_engine.MAX_INPUT_CHARS + 1)
        res = jev_mcp.guardrail_command(huge)
        assert "error" in res
        assert res["error"]["code"] == "INVALID_INPUT"
        assert res["error"]["retryable"] is False


class TestRootDirGuardrails:
    def test_root_dir_system_root_rejected(self):
        sys_root = "C:\\" if os.name == "nt" else "/"
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine._validate_root_dir(sys_root)
        assert "system directory" in str(exc_info.value)

    def test_root_dir_windows_rejected(self):
        if os.name == "nt":
            with pytest.raises(JevValidationError) as exc_info:
                jev_engine._validate_root_dir("C:\\Windows")
            assert "system directory" in str(exc_info.value)

    def test_root_dir_etc_rejected(self):
        if os.name != "nt":
            with pytest.raises(JevValidationError) as exc_info:
                jev_engine._validate_root_dir("/etc")
            assert "system directory" in str(exc_info.value)

    def test_root_dir_nonexistent_rejected(self, tmp_path):
        nonexistent = str(tmp_path / "does_not_exist_abc123")
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine._validate_root_dir(nonexistent)
        assert "does not exist or is not a directory" in str(exc_info.value)

    def test_root_dir_file_rejected(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hello", encoding="utf-8")
        with pytest.raises(JevValidationError) as exc_info:
            jev_engine._validate_root_dir(str(f))
        assert "does not exist or is not a directory" in str(exc_info.value)

    def test_root_dir_valid_dir_accepted(self, tmp_path):
        resolved = jev_engine._validate_root_dir(str(tmp_path))
        assert resolved == tmp_path.resolve()

    def test_find_agent_resources_system_dir_rejected(self):
        sys_root = "C:\\" if os.name == "nt" else "/"
        with pytest.raises(JevValidationError):
            jev_engine.find_agent_resources("task", sys_root)

    def test_select_target_files_system_dir_rejected(self):
        sys_root = "C:\\" if os.name == "nt" else "/"
        with pytest.raises(JevValidationError):
            jev_engine.select_target_files("task", sys_root)

    def test_mcp_search_skills_system_dir_returns_error_envelope(self):
        sys_root = "C:\\" if os.name == "nt" else "/"
        res = jev_mcp.search_agent_skills("task", sys_root)
        assert "error" in res
        assert res["error"]["code"] == "INVALID_INPUT"
        assert res["error"]["retryable"] is False


class TestSettingsLookup:
    def test_find_settings_files_includes_script_dir_when_cwd_differs(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        candidates = jev_engine._find_settings_files()
        script_dir = Path(jev_engine.__file__).resolve().parent
        expected = script_dir / "jevs_settings.json"
        if expected.exists():
            assert expected in candidates

    def test_find_settings_files_cwd_precedes_script_dir(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        local_settings = tmp_path / "jevs_settings.json"
        local_settings.write_text('{"enable_model_routing": true}', encoding="utf-8")
        candidates = jev_engine._find_settings_files()
        assert candidates[0] == local_settings
        script_dir = Path(jev_engine.__file__).resolve().parent
        expected_script = script_dir / "jevs_settings.json"
        if expected_script.exists():
            assert expected_script in candidates
            assert candidates.index(local_settings) < candidates.index(expected_script)

    def test_find_config_files_includes_script_dir(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        candidates = jev_engine._find_config_files()
        script_dir = Path(jev_engine.__file__).resolve().parent
        expected = script_dir / "opencode.json"
        if expected.exists():
            assert expected in candidates

    def test_load_jev_settings_finds_script_dir_settings_from_different_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        settings = jev_engine.load_jev_settings()
        script_dir = Path(jev_engine.__file__).resolve().parent
        expected_file = script_dir / "jevs_settings.json"
        if expected_file.exists():
            assert settings["source"] == str(expected_file)