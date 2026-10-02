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

    It was four: the flat `file`/`content` keys, plus `primary` and
    `resources[0]` as byte-identical copies of each other. Removing the flat keys
    left two (Phase 1, docs/perf-baseline.md); `primary` is now an identity view —
    which file won — so the body appears exactly once. Measured 6,957 → 4,142
    bytes on the live 118-skill workspace.
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
        assert res["primary"]["name"] == res["resources"][0]["name"]

    def test_primary_carries_no_content(self, tmp_path):
        """The identity view is the whole point: a `content` key here would put
        the body back a second time in the same document."""
        res = jev_engine.find_agent_resources("alpha helpers", str(self._workspace(tmp_path)))
        assert "content" not in res["primary"], "primary must not duplicate resources[0]"
        assert res["resources"][0]["content"], "the body still has to reach the caller"

    def test_envelope_is_under_60_percent_of_the_phase_1_size(self, tmp_path):
        res = jev_engine.find_agent_resources("alpha helpers", str(self._workspace(tmp_path)))
        size = len(json.dumps(res, default=str).encode("utf-8"))
        phase_1_bytes = 44_198
        assert res["primary"], "expected a winning resource to measure"
        assert size < phase_1_bytes * 0.6, f"{size} bytes is not under 60% of {phase_1_bytes}"

    def test_resource_body_appears_exactly_once(self, tmp_path):
        res = jev_engine.find_agent_resources("alpha helpers", str(self._workspace(tmp_path)))
        dumped = json.dumps(res, default=str)
        # Once, in `resources[0]` - the single documented place.
        assert dumped.count("ALPHA-UNIQUE-BODY-LINE") == 1


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


class TestSelectMcpToolsEnvelopeKeys:
    """Five branches, one key set: nothing may be missing on the paths that
    decided nothing."""

    ROSTER = [
        {"name": "jev-engine", "description": "Jev decision engine", "tools": [{"name": "guardrail_command", "description": "is this command safe"}]},
        {"name": "git", "description": "git repositories", "tools": [{"name": "git_commit", "description": "create a commit"}]},
        {"name": "github", "description": "github issues and pull requests", "tools": [{"name": "create_pr", "description": "open a pull request"}]},
    ]

    def _stub(self, monkeypatch, server_probs, tool_probs, relevance, decisive, confidence=0.9):
        from typesafe_sdk import ChoiceAnswer, NoulAnswer, SystemOneResponse, Usage

        def _fake_request(state, questions):
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
                    choice=max(tool_probs, key=lambda k: tool_probs[k]),
                    probabilities=dict(tool_probs),
                    confidence=0.9,
                )
            return (
                SystemOneResponse(model="jev-test", answers=answers, usage=Usage(input_tokens=8, output_tokens=4)),
                {"truncated": False, "coverage": {}},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _fake_request)

    def test_keys_identical_across_branches(self, tmp_path, monkeypatch):
        none_prob = {"git": 0.0, "github": 0.0, "none": 1.0}
        tools_none = {"git::git_commit": 0.0, "github::create_pr": 0.0, "none": 1.0}

        # 1. Nothing to choose from: the roster is empty.
        res_no_candidates = jev_engine.select_mcp_tools("fix a typo", [], str(tmp_path))
        assert res_no_candidates["exists"] == "no_candidates"

        # 2. The Choice declines.
        self._stub(monkeypatch, none_prob, tools_none, relevance=0.05, decisive=0.05)
        res_absent = jev_engine.select_mcp_tools("fix a typo", self.ROSTER, str(tmp_path))

        # 3. The Choice wins among servers that do not fit.
        forced = {"git": 0.6, "github": 0.3, "none": 0.1}
        self._stub(monkeypatch, forced, {"git::git_commit": 0.9, "github::create_pr": 0.05, "none": 0.05}, relevance=0.2, decisive=0.9)
        res_partial = jev_engine.select_mcp_tools("fix a typo", self.ROSTER, str(tmp_path))

        # 4. Two servers are equally usable.
        self._stub(monkeypatch, {"git": 0.45, "github": 0.45, "none": 0.1}, {"git::git_commit": 0.5, "github::create_pr": 0.5, "none": 0.0}, relevance=0.9, decisive=0.9)
        res_ambiguous = jev_engine.select_mcp_tools("commit and open a pr", self.ROSTER, str(tmp_path))

        # 5. The normal answer.
        self._stub(monkeypatch, {"git": 0.8, "github": 0.1, "none": 0.1}, {"git::git_commit": 0.8, "github::create_pr": 0.1, "none": 0.1}, relevance=0.9, decisive=0.9)
        res_answered = jev_engine.select_mcp_tools("commit the staged changes", self.ROSTER, str(tmp_path))

        keys = {name: set(result.keys()) for name, result in [
            ("no_candidates", res_no_candidates),
            ("absent", res_absent),
            ("partial", res_partial),
            ("ambiguous", res_ambiguous),
            ("answered", res_answered),
        ]}
        reference = keys["answered"]
        for name, branch in keys.items():
            assert branch == reference, f"{name} differs: {branch ^ reference}"

        assert res_absent["exists"] == "absent"
        assert res_partial["exists"] == "partial"
        assert res_ambiguous["exists"] == "ambiguous"
        assert res_answered["exists"] == "answered"

        for result in (res_no_candidates, res_absent, res_partial, res_ambiguous):
            assert result["primary"] is None
            assert result["tools"] == []
            assert result["matched"] is False
        assert res_no_candidates["confidence"] is None
        assert res_no_candidates["coverage"] is not None
        assert res_answered["primary"]["server"] == "git"
        assert [entry["tool"] for entry in res_answered["tools"]] == ["git_commit"]
        assert res_ambiguous["action"] != "auto"


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
