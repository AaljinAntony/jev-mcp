"""Offline mock-mode tool tests (no TYPESAFE_API_KEY required)."""

import json
import os
from pathlib import Path
import pytest

from jev_errors import JevToolError, JevValidationError
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
        with pytest.raises(JevToolError) as exc_info:
            jev_mcp._run("guardrail_command", lambda: jev_engine.verify_command("x"))
        envelope = exc_info.value.envelope
        assert "error" in envelope
        assert envelope["error"]["code"] == "INVALID_RESPONSE"
        assert "safe" not in envelope

    def test_tool_returns_json_serializable(self):
        result = jev_engine.verify_command("git status")
        json.dumps(result)  # must not raise

    def test_verify_command_logs_round(self, monkeypatch):
        """After BUG-2 fix, verify_command should log a round via _request()."""
        logged = []
        original_log_round = jev_engine.log_round
        monkeypatch.setattr(
            jev_engine,
            "log_round",
            lambda *a, **kw: logged.append(kw) or original_log_round(*a, **kw),
        )
        jev_engine.verify_command("git status")
        assert len(logged) >= 1, "verify_command should call log_round via _request()"

    def test_empty_command(self):
        """Empty command should still return a valid envelope."""
        result = jev_engine.verify_command("")
        assert "safe" in result
        assert "action" in result


class TestSearchAgentSkills:
    def test_mock_offline_with_files(self, tmp_path, monkeypatch):
        skills = tmp_path / ".agents" / "skills"
        (skills / "tool-x").mkdir(parents=True)
        (skills / "tool-y").mkdir(parents=True)
        (skills / "tool-x" / "SKILL.md").write_text(
            "---\ndescription: theme and layout helpers\n---\n# tool-x\n", encoding="utf-8"
        )
        (skills / "tool-y" / "SKILL.md").write_text(
            "---\ndescription: ui dialog navigation\n---\n# tool-y\n", encoding="utf-8"
        )

        result = jev_engine.find_agent_resources("theme layout helper", str(tmp_path))
        assert "matched" in result and isinstance(result["matched"], bool)
        assert "count" in result
        assert "primary" in result
        assert "ranked" in result and isinstance(result["ranked"], list)
        assert result["ranked"][0]["file"] == result["primary"]["file"]
        assert "action" in result and result["action"] in {"auto", "review", "escalate"}
        assert "confidence" in result
        assert "model" in result and result["model"].endswith("+mock")
        assert "usage" in result
        # the front-matter description is what reached the model
        assert result["coverage"]["candidate_fields"]["evaluated_chars"] > 0
        assert result["candidates_considered"] == 2
        assert result["candidates_truncated"] is False
        assert "candidates_truncated" not in result["reason_codes"]

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

    def test_single_question_and_front_matter_evidence(self, tmp_path, monkeypatch):
        skills = tmp_path / ".agents" / "skills"
        for name, description in [("alpha", "release notes drafting"), ("beta", "sqlite index tuning")]:
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nbody text\n",
                encoding="utf-8",
            )

        seen = {}

        def _spy(state, questions):
            seen["keys"] = set(questions.keys())
            seen["criteria"] = dict(questions["primary"].criteria)
            seen["state"] = state
            return (
                _fake_response({
                    "primary": ChoiceAnswer(
                        choice=".agents/skills/beta/SKILL.md",
                        confidence=0.9,
                        probabilities={
                            ".agents/skills/alpha/SKILL.md": 0.05,
                            ".agents/skills/beta/SKILL.md": 0.9,
                            "none": 0.05,
                        },
                    )
                }),
                {"truncated": False, "coverage": {}},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _spy)
        result = jev_engine.find_agent_resources("tune the database index", str(tmp_path))

        # secondary/tertiary are gone: one ranking, one question
        assert seen["keys"] == {"primary"}
        assert "none" in seen["criteria"]
        assert seen["criteria"][".agents/skills/beta/SKILL.md"] == "sqlite index tuning"
        assert seen["state"]["task"] == "tune the database index"
        assert seen["state"]["candidates_considered"] == 2
        # The flat `file`/`content` duplicates were removed: `primary` and
        # `resources[0]` are the only copies of the winning resource.
        assert "file" not in result
        assert "content" not in result
        assert result["primary"]["file"] == ".agents/skills/beta/SKILL.md"
        assert result["primary"]["file"] == result["resources"][0]["file"]
        assert result["primary_probability"] == 0.9
        assert [r["file"] for r in result["ranked"]][0] == ".agents/skills/beta/SKILL.md"
        # one read per candidate: the returned content is the cached text
        assert "sqlite index tuning" in result["primary"]["content"]

    def test_candidate_truncation_reported_and_degrades_action(self, tmp_path, monkeypatch):
        skills = tmp_path / ".agents" / "skills"
        for i in range(4):
            (skills / f"s{i}").mkdir(parents=True)
            (skills / f"s{i}" / "SKILL.md").write_text(f"skill {i} body", encoding="utf-8")

        monkeypatch.setattr(jev_engine, "MAX_CHOICE_OPTIONS", 2)
        result = jev_engine.find_agent_resources("skill 1", str(tmp_path))
        assert result["candidates_considered"] == 4
        assert result["candidates_evaluated"] == 2
        assert result["candidates_truncated"] is True
        assert "candidates_truncated" in result["reason_codes"]
        assert result["action"] != "auto"
        assert result["coverage"]["candidate_fields"]["complete"] is False

    def test_dict_shaped_answer_sibling_augmentation(self, tmp_path, monkeypatch):
        skills = tmp_path / ".agents" / "skills"
        (skills / "opt_a").mkdir(parents=True)
        (skills / "opt_b").mkdir(parents=True)
        f_a = skills / "opt_a" / "SKILL.md"
        f_b = skills / "opt_b" / "SKILL.md"
        f_a.write_text("# Skill A", encoding="utf-8")
        f_b.write_text("# Skill B", encoding="utf-8")
        rel_a = f_a.relative_to(tmp_path).as_posix()
        rel_b = f_b.relative_to(tmp_path).as_posix()

        raw_res = SystemOneResponse(
            model="jev-test",
            answers={
                "primary": {
                    "type": "choice",
                    "choice": rel_a,
                    "confidence": 0.8,
                    "probabilities": {rel_a: 0.8, rel_b: 0.2},
                },
            },
            usage=Usage(input_tokens=10, output_tokens=5),
        )
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

        monkeypatch.setattr(jev_engine, "_request", lambda state, questions: (raw_res, fitted_mock, cfg_mock))

        res = jev_engine.find_agent_resources("task", str(tmp_path))
        assert res["matched"] is True
        resource_files = [r["file"] for r in res["resources"]]
        assert rel_a in resource_files
        assert rel_b in resource_files

    def test_root_dir_nonexistent_returns_empty(self):
        """Non-existent root_dir is rejected as invalid input by guardrails."""
        with pytest.raises(JevValidationError):
            jev_engine.find_agent_resources("anything", "/nonexistent/path/12345")
        with pytest.raises(JevToolError) as exc_info:
            jev_mcp.search_agent_skills("anything", "/nonexistent/path/12345")
        data = json.loads(str(exc_info.value))
        assert "error" in data
        assert data["error"]["code"] == "INVALID_INPUT"

    def test_symlink_outside_root_skipped(self, tmp_path):
        """Files reached via symlinks outside root should not crash relative_to()."""
        skills = tmp_path / ".agents" / "skills" / "symlinked"
        skills.mkdir(parents=True)
        # Create a symlink pointing outside tmp_path
        external = tmp_path.parent / "external_skill"
        external.mkdir(exist_ok=True)
        (external / "SKILL.md").write_text("# External", encoding="utf-8")
        try:
            link = skills / "ext_link"
            link.symlink_to(external)
        except OSError:
            pytest.skip("Cannot create symlinks on this OS/filesystem")
        # Should not raise ValueError from relative_to()
        result = jev_engine.find_agent_resources("external skill", str(tmp_path))
        # Result should work, just might not include the external file
        assert isinstance(result["matched"], bool)


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
                    ),
                    "is_relevant": NoulAnswer(noul=0.1),
                }
            ),
        )
        result = jev_engine.select_target_files("buy groceries", str(tmp_path))
        assert result["matched"] is False
        assert result["files"] == []
        assert result["exists"] in {"absent", "partial"}
        assert result["relevance_prob"] == 0.1

    def test_confident_choice_with_low_presence_is_partial(self, tmp_path, monkeypatch):
        # The presence Noul is the whole point: a forced winner among poor options
        # must not read as a match, even when the Choice is confident.
        target = tmp_path / "src" / "core.py"
        target.parent.mkdir(parents=True)
        target.write_text("def run(): pass", encoding="utf-8")

        monkeypatch.setattr(
            jev_engine,
            "execute_system_one",
            lambda *a, **k: _fake_response(
                {
                    "target_file": ChoiceAnswer(
                        choice="src/core.py",
                        confidence=0.9,
                        probabilities={"src/core.py": 0.9, "none": 0.1},
                    ),
                    "is_relevant": NoulAnswer(noul=0.2),
                }
            ),
        )
        result = jev_engine.select_target_files("something unrelated", str(tmp_path))
        assert result["exists"] == "partial"
        assert result["matched"] is False
        assert result["files"] == []

    def test_presence_noul_present_for_every_call(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
        seen = {}

        def _spy(state, questions):
            seen["keys"] = set(questions.keys())
            seen["criteria"] = dict(questions["target_file"].criteria)
            seen["state"] = state
            return (
                _fake_response({
                    "target_file": ChoiceAnswer(
                        choice="a.py", confidence=0.9, probabilities={"a.py": 0.9, "none": 0.1}
                    ),
                    "is_relevant": NoulAnswer(noul=0.8),
                }),
                {"truncated": False, "coverage": {}},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _spy)
        jev_engine.select_target_files("print a", str(tmp_path))
        assert seen["keys"] == {"target_file", "is_relevant"}
        assert "none" in seen["criteria"]
        # criteria carry the file's opening content, not one shared sentence
        assert "print" in seen["criteria"]["a.py"]
        assert seen["criteria"]["a.py"].startswith("a.py")
        assert seen["state"]["task"] == "print a"

    def test_discover_files_git_returns_none_for_nongit(self, tmp_path):
        assert jev_engine._discover_files_git(tmp_path, {".png"}, 10) is None

    def test_discover_files_git_parses_git_output(self, tmp_path, monkeypatch):
        # Create a mock .git directory in tmp_path
        (tmp_path / ".git").mkdir()
        # Mock subprocess.run
        import subprocess

        fake_files = [
            "src/main.py",
            "src\\utils.py",
            "node_modules/pkg/index.js",
            "assets/logo.png",
            "build/bundle.js",
            "README.md",
        ]
        # Create the fake files so (root / p).is_file() returns True
        for f in fake_files:
            p = tmp_path / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("ok", encoding="utf-8")

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=a[0], returncode=0, stdout="\n".join(fake_files)
            ),
        )

        ignore_dirs = {"node_modules", "build"}
        ignore_exts = {".png"}
        candidates = jev_engine._discover_files_git(
            tmp_path, ignore_exts, max_count=10, ignore_dirs=ignore_dirs
        )
        assert candidates == ["src/main.py", "src/utils.py", "README.md"]

    def test_discover_files_git_handles_failure(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        import subprocess

        # Non-zero exit code
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(args=a[0], returncode=128, stdout="", stderr="fatal"),
        )
        assert jev_engine._discover_files_git(tmp_path, {".png"}, 10) is None

        # TimeoutExpired
        def _raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        assert jev_engine._discover_files_git(tmp_path, {".png"}, 10) is None

    def test_select_target_files_uses_git_candidates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            jev_engine,
            "_discover_files_git",
            lambda root, exts, count, dirs=None: ["git_discovered.py"],
        )
        result = jev_engine.select_target_files("edit git_discovered", str(tmp_path))
        assert result["matched"] is True
        assert result["files"] == ["git_discovered.py"]

    def test_empty_task(self, tmp_path):
        """Empty task should still return a valid envelope."""
        (tmp_path / "file.py").write_text("pass", encoding="utf-8")
        result = jev_engine.select_target_files("", str(tmp_path))
        assert "matched" in result


class TestEvidenceBudget:
    """The Choice has to see real content, and getting it must stay bounded."""

    def test_selected_skill_is_read_exactly_once(self, tmp_path, monkeypatch):
        skills = tmp_path / ".agents" / "skills" / "alpha"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "---\ndescription: alpha summary\n---\n" + "body line\n" * 500,
            encoding="utf-8",
        )
        (tmp_path / ".agents" / "skills" / "beta").mkdir(parents=True)
        (tmp_path / ".agents" / "skills" / "beta" / "SKILL.md").write_text(
            "---\ndescription: beta summary\n---\nbeta body\n", encoding="utf-8"
        )

        opens = []
        real_open = jev_engine.read_head

        def _counting_read(path, *args, **kwargs):
            opens.append(str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(jev_engine, "read_head", _counting_read)
        result = jev_engine.find_agent_resources("alpha summary", str(tmp_path))
        assert result["primary"]["file"] == ".agents/skills/alpha/SKILL.md"
        assert len(opens) == len(set(opens)), "a candidate was read more than once"
        # the returned content is the cached text, not a second read
        assert "body line" in result["primary"]["content"]

    def test_target_file_previews_are_bounded(self, tmp_path, monkeypatch):
        names = [f"mod_{i:03d}.py" for i in range(200)]
        for name in names:
            (tmp_path / name).write_text("# module\ndef process(): pass\n", encoding="utf-8")
        monkeypatch.setattr(jev_engine, "MAX_PREVIEW_READS", 10)
        monkeypatch.setattr(
            jev_engine, "_discover_files_git", lambda root, exts, count, dirs=None: names
        )

        captured = {}

        def _spy(state, questions):
            captured["criteria"] = dict(questions["target_file"].criteria)
            return (
                _fake_response({
                    "target_file": ChoiceAnswer(
                        choice=names[0], confidence=0.9, probabilities={**{n: 0.005 for n in names}, names[0]: 0.5, "none": 0.0}
                    ),
                    "is_relevant": NoulAnswer(noul=0.9),
                }),
                {"truncated": False, "coverage": {}},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _spy)
        result = jev_engine.select_target_files("process", str(tmp_path))

        fields = result["coverage"]["candidate_fields"]
        assert fields["previews_built"] == 10
        assert fields["previews_skipped"] == len(names) - 10
        assert fields["candidates_considered"] == len(names)
        # skipped candidates keep a selectable path, they are never dropped
        assert captured["criteria"][names[50]] == names[50]
        assert "def process" in captured["criteria"][names[0]]

    def test_binary_file_falls_back_to_path(self, tmp_path, monkeypatch):
        (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\x00\x00\x00" + b"\x00" * 64)
        (tmp_path / "app.py").write_text("print('app')\n", encoding="utf-8")
        monkeypatch.setattr(jev_engine, "_discover_files_git", lambda *a, **k: ["logo.png", "app.py"])

        captured = {}

        def _spy(state, questions):
            captured.update(questions["target_file"].criteria)
            return (
                _fake_response({
                    "target_file": ChoiceAnswer(
                        choice="app.py", confidence=0.9, probabilities={"app.py": 0.9, "logo.png": 0.05, "none": 0.05}
                    ),
                    "is_relevant": NoulAnswer(noul=0.9),
                }),
                {"truncated": False, "coverage": {}},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _spy)
        jev_engine.select_target_files("print", str(tmp_path))
        assert captured["logo.png"] == "logo.png"
        assert "print" in captured["app.py"]


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
        with pytest.raises(JevToolError) as exc_info:
            jev_mcp.guardrail_command(huge)
        data = json.loads(str(exc_info.value))
        assert "error" in data
        assert data["error"]["code"] == "INVALID_INPUT"
        assert data["error"]["retryable"] is False


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
        with pytest.raises(JevToolError) as exc_info:
            jev_mcp.search_agent_skills("task", sys_root)
        data = json.loads(str(exc_info.value))
        assert "error" in data
        assert data["error"]["code"] == "INVALID_INPUT"
        assert data["error"]["retryable"] is False


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


class TestInputValidation:
    def test_oversized_command_returns_error(self):
        """Commands exceeding MAX_INPUT_CHARS should return INVALID_INPUT."""
        huge_command = "x" * 200_000
        # This should be caught by _run() and raised as JevToolError
        with pytest.raises(JevToolError) as exc_info:
            jev_mcp._run(
                "guardrail_command",
                lambda: jev_engine.verify_command(huge_command),
            )
        result = exc_info.value.envelope
        assert "error" in result
        assert result["error"]["code"] == "INVALID_INPUT"
    def test_oversized_task_returns_error(self):
        huge_task = "x" * 200_000
        with pytest.raises(JevToolError) as exc_info:
            jev_mcp._run(
                "select_model_tier",
                lambda: jev_engine.select_model_tier(huge_task),
            )
        result = exc_info.value.envelope
        assert "error" in result
        assert result["error"]["code"] == "INVALID_INPUT"