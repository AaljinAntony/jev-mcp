"""Table-driven tests verifying identical envelope keys across all branches of every tool."""

import json
import pytest

from typesafe_sdk import NoulAnswer
import jev_engine


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


class TestFindAgentResourcesEnvelopeKeys:
    def test_keys_identical_across_branches(self, tmp_path, stub_choice):
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

        # 3. No match branch (force the answer to `none`)
        stub_choice("none", {rel1: 0.05, "none": 0.95})
        res_no_match = jev_engine.find_agent_resources("unknown", str(skills_dir))

        keys_no_candidates = set(res_no_candidates.keys())
        keys_match = set(res_match.keys())
        keys_no_match = set(res_no_match.keys())

        assert keys_no_candidates == keys_match, f"Diff: {keys_no_candidates ^ keys_match}"
        assert keys_match == keys_no_match, f"Diff: {keys_match ^ keys_no_match}"
        assert res_no_candidates["coverage"] is not None
        assert res_no_candidates["action"] == "auto"
        assert res_no_candidates["confidence"] is None


class TestSkillEnvelopeHasNoDuplicates:
    """The 6,000-character resource body must be serialized once, not four times.

    `primary` is `resources[0]`, and the old flat `file`/`content` keys were two
    more copies of the same blob in the same JSON document. Phase 1 measured
    44,198 bytes for this envelope (docs/perf-baseline.md).
    """

    def _workspace(self, tmp_path):
        for name in ("alpha", "beta"):
            d = tmp_path / ".agents" / "skills" / name
            d.mkdir(parents=True)
            unique = "ALPHA-UNIQUE-BODY-LINE" if name == "alpha" else "BETA-UNIQUE-BODY-LINE"
            (d / "SKILL.md").write_text(
                f"---\ndescription: {name} helpers\n---\n{unique}\n" + f"{name} filler line\n" * 900,
                encoding="utf-8",
            )
        return tmp_path

    def test_flat_file_and_content_keys_are_gone(self, tmp_path):
        res = jev_engine.find_agent_resources("alpha helpers", str(self._workspace(tmp_path)))
        assert "file" not in res
        assert "content" not in res
        assert res["primary"]["file"] == res["resources"][0]["file"]
        assert res["primary"]["content"] == res["resources"][0]["content"]

    def test_envelope_is_under_60_percent_of_the_phase_1_size(self, tmp_path):
        res = jev_engine.find_agent_resources("alpha helpers", str(self._workspace(tmp_path)))
        size = len(json.dumps(res, default=str).encode("utf-8"))
        phase_1_bytes = 44_198
        assert res["primary"], "expected a winning resource to measure"
        assert size < phase_1_bytes * 0.6, f"{size} bytes is not under 60% of {phase_1_bytes}"

    def test_resource_body_appears_exactly_twice(self, tmp_path):
        res = jev_engine.find_agent_resources("alpha helpers", str(self._workspace(tmp_path)))
        dumped = json.dumps(res, default=str)
        # Once in `primary`, once in `resources[0]` — the two documented places.
        assert dumped.count("ALPHA-UNIQUE-BODY-LINE") == 2


class TestSelectTargetFilesEnvelopeKeys:
    def test_keys_identical_across_branches(self, tmp_path, monkeypatch, stub_choice):
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

        # 3. The `none` branch: the Choice declines and the presence Noul agrees.
        stub_choice(
            "none",
            {"main.py": 0.1, "util.py": 0.1, "none": 0.8},
            key="target_file",
            confidence=0.7,
            extra={"is_relevant": NoulAnswer(noul=0.1)},
        )
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
    def test_keys_identical_across_branches(self, monkeypatch, stub_choice):
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
        stub_choice(
            "balanced",
            {"fast": 0.33, "balanced": 0.34, "frontier": 0.33},
            key="tier",
            confidence=0.1,
        )
        res_low_conf = jev_engine.select_model_tier("ambiguous task")

        keys_disabled = set(res_disabled.keys())
        keys_on = set(res_on.keys())
        keys_low_conf = set(res_low_conf.keys())

        assert keys_disabled == keys_on, f"Diff: {keys_disabled ^ keys_on}"
        assert keys_on == keys_low_conf, f"Diff: {keys_on ^ keys_low_conf}"
        assert res_disabled["coverage"] is not None
        assert res_low_conf["action"] == "escalate"
