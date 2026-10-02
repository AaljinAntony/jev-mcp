"""The prefilter: which candidates spend the preview budget.

`MAX_PREVIEWED_CANDIDATES` is a quality/cost trade, and these tests pin the half
that keeps it safe. The claim is narrow and checkable: *ranking never removes an
option and never picks a worse one by accident.* A candidate that loses its
preview keeps its path as evidence and stays selectable, so a task that shares no
vocabulary with any filename degrades to exactly the old "first N" behaviour.

The accuracy side is not asserted here — it is measured, not unit-testable. See
`scripts/eval_routing.py` and the Phase 2 row in `docs/perf-baseline.md`:
live top-1 0.6875 → 0.75, top-3 recall held at 1.0, mean input tokens
12,949 → 7,871.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import candidates  # noqa: E402
import jev_engine  # noqa: E402

TREE = [
    "reference/burnigtm-jev-mcp/src/index.ts",
    "reference/burnigtm-jev-mcp/src/lib.ts",
    "reference/jkudish-jev-mcp/src/index.ts",
    "docs/ARCHITECTURE.md",
    "docs/perf-baseline.md",
    "limits.py",
    "config.py",
    "scripts/bench_jev.py",
    ".agents/skills/ponytail/SKILL.md",
    ".agents/skills/typesafe-ai/SKILL.md",
    "README.md",
]


class TestTerms:
    @pytest.mark.parametrize(
        "name",
        ["readHead", "read_head", "read-head", "read/head", "read.head", "READ HEAD"],
    )
    def test_every_separator_and_case_reaches_the_same_terms(self, name):
        assert {"read", "head"} <= jev_engine._terms(name)

    def test_an_extension_does_not_hide_the_stem(self):
        """Without splitting the dot, `limits.py` is one opaque term and never
        matches a task that says "limits"."""
        assert "limits" in jev_engine._terms("limits.py")

    def test_camel_case_splits_into_both_halves(self):
        assert {"token", "budget"} <= jev_engine._terms("tokenBudget")


class TestLexicalScore:
    def test_a_matching_stem_scores(self):
        assert jev_engine._lexical_score("limits.py", {"limits"}) == 1

    def test_a_generic_directory_does_not_score(self):
        """Otherwise every file under `src/` matches a task that says "src" and
        the ranking collapses to a no-op on a deep tree."""
        assert jev_engine._lexical_score("src/anything.py", {"src"}) == 0
        assert jev_engine._lexical_score("docs/x.md", {"docs"}) == 0

    def test_a_leading_dot_directory_is_recognised_as_generic(self):
        """`.agents` has to reduce to `agents`; `rsplit('.')` on it yields an
        empty stem, which used to let the whole segment through."""
        assert jev_engine._lexical_score(".agents/skills/x/SKILL.md", {"agents"}) == 0

    def test_a_real_directory_still_scores_through_its_parent(self):
        assert jev_engine._lexical_score("skills/ponytail/SKILL.md", {"ponytail"}) == 1

    def test_a_skill_md_stem_is_not_evidence(self):
        assert jev_engine._lexical_score("a/b/SKILL.md", {"skill"}) == 0

    def test_no_task_terms_scores_nothing(self):
        assert jev_engine._lexical_score("limits.py", set()) == 0


class TestRankCandidates:
    def test_a_matching_candidate_comes_first(self):
        order = jev_engine._rank_candidates(TREE, "optimize the token used by jev")
        best = min(order, key=lambda p: order[p])
        assert "jev-mcp" in best or "bench_jev" in best

    def test_ranking_is_a_permutation_of_its_input(self):
        order = jev_engine._rank_candidates(TREE, "optimize the token")
        assert sorted(order.values()) == list(range(len(TREE)))
        assert set(order) == set(TREE)

    def test_unrelated_task_degrades_to_discovery_order(self):
        """The safety property. A task sharing no vocabulary with any filename
        must behave exactly as it did before the prefilter existed."""
        assert list(jev_engine._rank_candidates(TREE, "zzz qqq")) == TREE

    def test_an_empty_task_degrades_to_discovery_order(self):
        assert list(jev_engine._rank_candidates(TREE, "")) == TREE

    def test_ties_keep_discovery_order(self):
        flat = [f"file{i}.py" for i in range(5)]
        assert list(jev_engine._rank_candidates(flat, "nothing matches")) == flat

    def test_a_task_matching_nothing_still_ranks_every_candidate(self):
        order = jev_engine._rank_candidates(TREE, "kubernetes helm chart")
        assert len(order) == len(TREE)


class TestBuildCriteriaPreviewBound:
    def _reads(self, log):
        def _read(path):
            log.append(path)
            return f"---\ndescription: about {path}\n---\nbody of {path}"

        return _read

    def test_a_candidate_outside_the_preview_set_keeps_its_path(self):
        log = []
        criteria = candidates.build_criteria(
            ["a.md", "b.md"], self._reads(log), preview_paths={"a.md"}
        )
        # `a.md` is not a SKILL.md, so its evidence is the markdown body rather
        # than the front-matter `description:`.
        assert criteria["a.md"] == "body of a.md"
        assert criteria["b.md"] == "b.md", "an unpreviewed candidate falls back to its path"
        assert log == ["a.md"], "b.md was never read"

    def test_every_candidate_keeps_its_option(self):
        """A Choice cannot pick a value it was never given, so the bound must
        narrow the evidence and never the options."""
        paths = [f"c{i}.md" for i in range(50)]
        criteria = candidates.build_criteria(
            paths, self._reads([]), preview_paths=set(paths[:5])
        )
        assert list(criteria) == paths

    def test_an_unreadable_candidate_is_distinguishable_from_an_unpreviewed_one(self):
        """`UNREADABLE` is a fact about the file; a bare path is a fact about the
        budget. Conflating them would report a read failure that never happened."""
        def _only_a(path):
            return "text" if path == "a.md" else ""

        criteria = candidates.build_criteria(
            ["a.md", "b.md"], _only_a, preview_paths={"a.md", "b.md"}
        )
        assert criteria["b.md"] == candidates.UNREADABLE

    def test_no_preview_bound_reads_everything(self):
        log = []
        criteria = candidates.build_criteria(["a.md", "b.md"], self._reads(log))
        assert log == ["a.md", "b.md"]
        assert criteria["a.md"] == "body of a.md"


class TestPreviewBudgetWiring:
    def test_the_workspace_file_path_ranks_by_task(self, tmp_path, monkeypatch):
        """`select_target_files` must spend its read budget on task-relevant
        files rather than the first N in discovery order."""
        for name in ("alpha_relevant.py", "filler_one.py", "filler_two.py"):
            (tmp_path / name).write_text("# code\n", encoding="utf-8")
        names = ["filler_one.py", "filler_two.py", "alpha_relevant.py"]
        monkeypatch.setattr(
            jev_engine, "_discover_candidates", lambda *a, **k: list(names)
        )
        monkeypatch.setattr(jev_engine, "MAX_PREVIEWED_CANDIDATES", 1)
        seen = {}

        def _spy(state, questions):
            seen["criteria"] = dict(questions["target_file"].criteria)
            raise RuntimeError("stop after the criteria are built")

        monkeypatch.setattr(jev_engine, "_request", _spy)
        with pytest.raises(RuntimeError):
            jev_engine.select_target_files("find the alpha relevant file", str(tmp_path))

        with_body = [c for c, v in seen["criteria"].items() if v != c and c != "none"]
        assert with_body == ["alpha_relevant.py"], seen["criteria"]

    def test_the_budget_constant_is_the_documented_value(self):
        from limits import MAX_PREVIEWED_CANDIDATES

        assert MAX_PREVIEWED_CANDIDATES == 40

    def test_the_skill_path_reports_what_it_skipped(self, tmp_path, monkeypatch, stub_choice):
        """Coverage has to name the candidates that never got evidence, or a
        truncated request reads as a complete one."""
        skills = tmp_path / ".agents" / "skills"
        for i in range(6):
            (skills / f"s{i}").mkdir(parents=True)
            (skills / f"s{i}" / "SKILL.md").write_text(
                f"---\ndescription: skill {i}\n---\nbody {i}\n", encoding="utf-8"
            )
        stub_choice("s0/SKILL.md", {f".agents/skills/s{i}/SKILL.md": 0.9 if i == 0 else 0.02
                                    for i in range(6)} | {"none": 0.01})
        monkeypatch.setattr(jev_engine, "MAX_PREVIEWED_CANDIDATES", 2)
        res = jev_engine.find_agent_resources("skill 0", str(tmp_path))
        fields = res["coverage"]["candidate_fields"]
        assert fields["previews_built"] == 2
        assert fields["previews_skipped"] == 4
        assert fields["candidates_considered"] == 6
