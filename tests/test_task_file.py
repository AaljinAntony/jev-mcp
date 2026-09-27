"""Tests for `task_file`: a saved prompt or plan supplied as a file.

The user-facing case this exists for: the prompt lives in a file and the chat
only carries its path, so the judge used to be handed "do phase 2 of
.d/agent_plans/phase_2.md" and had nothing to reason from. The file is read by
the *engine*, never by the plugin, and under exactly the allowlist that governs
`root_dir` — an LLM-supplied path is an LLM-supplied `root_dir` with extra steps.
"""

import pytest

import jev_engine
from jev_errors import JevValidationError


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A project tree with one skill, plus a saved prompt and a plan."""
    root = tmp_path / "project"
    skill_dir = root / ".agents" / "skills" / "audio" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: audio\ndescription: bus architecture and crossfading\n---\n\n"
        "Use the bus layout.\n",
        encoding="utf-8",
    )
    (root / "plan.md").write_text(
        "# Phase 2\n\nOverhaul the audio bus and crossfade the music.\n",
        encoding="utf-8",
    )
    # The allowlist is pinned to the workspace so the "outside" cases below are
    # deterministic: CWD's ancestors would otherwise allow the whole drive.
    monkeypatch.setattr(jev_engine, "_allowed_roots", lambda: [root.resolve()])
    return root


def _captured_task(monkeypatch, chosen="none", probabilities=None):
    """Run one tool with a stubbed judge and return the task text it was given."""
    seen = {}

    def _fake_request(state, questions):
        from typesafe_sdk import ChoiceAnswer, SystemOneResponse, Usage

        key = "target_file" if "target_file" in questions else "primary"
        if key == "target_file":
            from typesafe_sdk import NoulAnswer

            answers = {
                "target_file": ChoiceAnswer(
                    choice=chosen, probabilities=probabilities or {chosen: 1.0}, confidence=0.9
                ),
                "is_relevant": NoulAnswer(noul=0.1),
            }
        else:
            answers = {
                "primary": ChoiceAnswer(
                    choice=chosen, probabilities=probabilities or {chosen: 1.0}, confidence=0.9
                )
            }
        seen["task"] = state.get("task") if isinstance(state, dict) else state
        return (
            SystemOneResponse(model="jev-test", answers=answers, usage=Usage(input_tokens=1, output_tokens=1)),
            {"truncated": False, "coverage": {}},
            jev_engine.get_config(),
        )

    monkeypatch.setattr(jev_engine, "_request", _fake_request)
    return seen


class TestTaskFileIsRead:
    def test_relative_task_file_resolves_against_root(self, workspace, monkeypatch):
        seen = _captured_task(monkeypatch)
        jev_engine.find_agent_resources("do it", str(workspace), task_file="plan.md")
        assert "Overhaul the audio bus" in seen["task"]
        assert "do it" in seen["task"], "the caller's own words survive"

    def test_absolute_task_file_is_accepted(self, workspace, monkeypatch):
        seen = _captured_task(monkeypatch)
        jev_engine.find_agent_resources("", str(workspace), task_file=str(workspace / "plan.md"))
        assert "Overhaul the audio bus" in seen["task"]

    def test_front_matter_is_stripped_from_the_question(self, workspace, monkeypatch):
        (workspace / "skill_prompt.md").write_text(
            "---\nname: x\ndescription: y\n---\n\nReal question here.\n", encoding="utf-8"
        )
        seen = _captured_task(monkeypatch)
        jev_engine.find_agent_resources("", str(workspace), task_file="skill_prompt.md")
        assert "Real question here." in seen["task"]
        assert "description: y" not in seen["task"], "front matter is not question text"

    def test_no_task_file_leaves_the_task_untouched(self, workspace, monkeypatch):
        seen = _captured_task(monkeypatch)
        jev_engine.find_agent_resources("just this", str(workspace))
        assert seen["task"] == "just this"

    def test_select_target_files_accepts_a_task_file(self, workspace, monkeypatch):
        (workspace / "audio_manager.gd").write_text("extends Node\n", encoding="utf-8")
        seen = _captured_task(monkeypatch)
        jev_engine.select_target_files("which files", str(workspace), task_file="plan.md")
        assert "Overhaul the audio bus" in seen["task"]
        assert "which files" in seen["task"]


class TestTaskFileIsBounded:
    def test_only_the_head_of_a_long_file_is_read(self, workspace, monkeypatch):
        (workspace / "long.md").write_text(
            "HEAD_MARKER\n" + ("filler line\n" * 20_000), encoding="utf-8"
        )
        seen = _captured_task(monkeypatch)
        jev_engine.find_agent_resources("", str(workspace), task_file="long.md")
        assert "HEAD_MARKER" in seen["task"]
        assert len(seen["task"]) <= jev_engine.MAX_TASK_FILE_CHARS + 200

    def test_a_binary_file_is_refused(self, workspace):
        blob = workspace / "sprite.png"
        blob.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000)
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file="sprite.png")
        assert "binary" in str(exc.value)

    def test_an_empty_file_is_refused(self, workspace):
        (workspace / "empty.md").write_text("   \n\n", encoding="utf-8")
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file="empty.md")
        assert "empty" in str(exc.value)

    def test_oversized_task_file_argument_is_rejected_before_any_io(self, workspace):
        huge = "x" * (jev_engine.MAX_INPUT_CHARS + 1)
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file=huge)
        assert "too long" in str(exc.value)


class TestTaskFileContainment:
    def test_a_file_outside_the_allowlist_is_refused(self, workspace, tmp_path, monkeypatch):
        outside = tmp_path / "elsewhere" / "secret.md"
        outside.parent.mkdir()
        outside.write_text("SECRET-PLAN-CONTENT", encoding="utf-8")
        # root_dir is allowed, the file next to it is not.
        monkeypatch.setattr(jev_engine, "_allowed_roots", lambda: [workspace.resolve()])
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file=str(outside))
        assert "outside the allowed roots" in str(exc.value)

    def test_traversal_out_of_the_root_is_refused(self, workspace, tmp_path):
        # The traversal target exists, so what fires is the containment check and
        # not the "no such file" one: this is the case that matters.
        (tmp_path / "secret.md").write_text("SECRET", encoding="utf-8")
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file="../secret.md")
        assert "outside the allowed roots" in str(exc.value)

    def test_a_directory_is_not_a_task_file(self, workspace):
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file=".agents")
        assert "not a file" in str(exc.value)

    def test_a_missing_file_is_refused(self, workspace):
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file="nope.md")
        assert "does not exist" in str(exc.value)

    def test_symlink_escaping_the_root_is_refused(self, workspace, tmp_path, monkeypatch):
        target = tmp_path / "outside.md"
        target.write_text("SECRET", encoding="utf-8")
        link = workspace / "link.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable here")
        monkeypatch.setattr(jev_engine, "_allowed_roots", lambda: [workspace.resolve()])
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", str(workspace), task_file="link.md")
        assert "outside the allowed roots" in str(exc.value)


class TestTaskFileThroughTheMcpTool:
    def test_the_tool_envelope_carries_the_error_not_a_traceback(self, workspace):
        import json

        import jev_mcp

        with pytest.raises(Exception) as exc:
            jev_mcp.search_agent_skills(task="x", root_dir=str(workspace), task_file="nope.md")
        data = json.loads(str(exc.value))
        assert data["error"]["code"] == "INVALID_INPUT"
        assert data["error"]["retryable"] is False
        assert "does not exist" in data["error"]["message"]

    def test_the_tool_accepts_the_new_argument(self, workspace, monkeypatch):
        seen = _captured_task(monkeypatch)
        import jev_mcp

        jev_mcp.search_agent_skills(task="do it", root_dir=str(workspace), task_file="plan.md")
        assert "Overhaul the audio bus" in seen["task"]
