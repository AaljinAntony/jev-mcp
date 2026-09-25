"""Unit tests for the candidate evidence extractor (no filesystem required)."""

import pytest

from candidates import (
    UNREADABLE,
    bound_candidates,
    build_criteria,
    candidate_preview,
    front_matter_split,
    looks_binary,
    markdown_preview,
    skill_description,
)
from limits import MAX_CANDIDATE_CHARS, TRUNCATION_MARKER

SKILL = """---
name: theme-toggles
description: >-
  Add and audit dark mode toggles across the settings screens.
  Use when a task mentions dark mode, themes, or colour schemes.
---

# Theme toggles

Body paragraph that explains the workflow in detail.
"""


class TestFrontMatterSplit:
    def test_splits_front_matter_from_body(self):
        front, body = front_matter_split(SKILL)
        assert "description:" in front
        assert body.lstrip().startswith("# Theme toggles")

    def test_no_front_matter_returns_whole_text(self):
        front, body = front_matter_split("# plain\n\ntext")
        assert front == ""
        assert body == "# plain\n\ntext"

    def test_unterminated_front_matter_is_not_swallowed(self):
        text = "---\ndescription: x\n\nno closing fence"
        front, body = front_matter_split(text)
        assert front == ""
        assert body == text

    def test_empty_text(self):
        assert front_matter_split("") == ("", "")


class TestSkillDescription:
    def test_folded_scalar_is_joined(self):
        description = skill_description(SKILL)
        assert "dark mode toggles" in description
        assert "colour schemes" in description
        assert ">" not in description

    def test_plain_scalar(self):
        assert skill_description("---\ndescription: run the tests\n---\nbody") == "run the tests"

    def test_quoted_scalar(self):
        assert skill_description('---\ndescription: "quoted value"\n---\n') == "quoted value"

    def test_missing_description_returns_empty(self):
        assert skill_description("---\nname: x\n---\n# body") == ""
        assert skill_description("# no front matter at all") == ""

    def test_description_stops_at_next_key(self):
        text = "---\ndescription: first value\nname: second\n---\n"
        assert skill_description(text) == "first value"


class TestMarkdownPreview:
    def test_drops_front_matter_and_syntax_noise(self):
        preview = markdown_preview(SKILL)
        assert "description:" not in preview
        assert not preview.startswith("#")
        assert "Body paragraph" in preview

    def test_strips_html_comments_and_quotes(self):
        preview = markdown_preview("<!-- hidden note -->\n> quoted line\ntext after")
        assert "hidden note" not in preview
        assert not preview.startswith(">")
        assert "quoted line" in preview

    def test_keeps_list_bullets_but_drops_table_pipes(self):
        preview = markdown_preview("| a | b |\n| --- | --- |\n1. first step\n- second step")
        assert "|" not in preview
        assert "first step" in preview
        assert "second step" in preview

    def test_truncates_with_marker(self):
        preview = markdown_preview("x" * (MAX_CANDIDATE_CHARS * 2), max_chars=100)
        assert len(preview) <= 100
        assert preview.endswith(TRUNCATION_MARKER)

    def test_empty_text(self):
        assert markdown_preview("") == ""


class TestLooksBinary:
    def test_nul_heavy_is_binary(self):
        assert looks_binary("abc\x00\x00\x00def") is True

    def test_text_is_not_binary(self):
        assert looks_binary("plain markdown text") is False
        assert looks_binary("") is False


class TestCandidatePreview:
    def test_skill_prefers_front_matter_description(self):
        assert candidate_preview(".agents/skills/x/SKILL.md", SKILL) == skill_description(SKILL)

    def test_skill_without_description_falls_back_to_preview(self):
        text = "---\nname: x\n---\n\nA plain explanation of what this skill does.\n"
        preview = candidate_preview(".agents/skills/x/SKILL.md", text)
        assert "plain explanation" in preview
        assert "name:" not in preview

    def test_non_skill_uses_markdown_preview(self):
        preview = candidate_preview("src/core.py", "# core\n\nimport os\n")
        assert "import os" in preview

    def test_binary_yields_no_preview(self):
        assert candidate_preview("assets/logo.png", "PNG\x00\x00\x00\x00") == ""


class TestBuildCriteria:
    def test_maps_every_path(self):
        criteria = build_criteria(["a/SKILL.md", "b/SKILL.md"], lambda p: SKILL)
        assert set(criteria) == {"a/SKILL.md", "b/SKILL.md"}
        assert "dark mode" in criteria["a/SKILL.md"]

    def test_unreadable_candidate_keeps_its_option(self):
        def _read(path):
            if path == "broken/SKILL.md":
                raise OSError("permission denied")
            return SKILL

        criteria = build_criteria(["ok/SKILL.md", "broken/SKILL.md"], _read)
        assert criteria["broken/SKILL.md"] == UNREADABLE
        assert "broken/SKILL.md" in criteria

    def test_empty_file_keeps_its_option(self):
        criteria = build_criteria(["blank/SKILL.md"], lambda p: "")
        assert criteria["blank/SKILL.md"] == UNREADABLE

    def test_total_budget_shrinks_per_candidate_share(self):
        paths = [f"skill-{i}/SKILL.md" for i in range(50)]
        criteria = build_criteria(
            paths,
            lambda p: "d" * 4000,
            max_chars=2000,
            total_chars=10_000,
        )
        assert len(criteria) == 50
        assert all(len(v) <= 2000 + 40 for v in criteria.values())
        assert sum(len(v) for v in criteria.values()) <= 10_000 + 50 * 40

    def test_small_candidate_set_gets_full_allowance(self):
        criteria = build_criteria(["a/SKILL.md"], lambda p: "d" * 4000)
        assert len(criteria["a/SKILL.md"]) == 2000


class TestBoundCandidates:
    def test_under_limit_is_not_truncated(self):
        kept, truncated = bound_candidates(["a", "b"], 5)
        assert kept == ["a", "b"]
        assert truncated is False

    def test_over_limit_reports_truncation(self):
        kept, truncated = bound_candidates(["a", "b", "c"], 2)
        assert kept == ["a", "b"]
        assert truncated is True

    def test_no_limit_keeps_everything(self):
        kept, truncated = bound_candidates(["a", "b"], 0)
        assert kept == ["a", "b"]
        assert truncated is False
