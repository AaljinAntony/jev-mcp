"""The workspace scanners must not repeat their work on an identical call.

`find_agent_resources` used to `rglob` every configured skill directory on every
call, and `select_target_files` forked a `git ls-files` subprocess every call.
Both results are pure path discovery, so both are cached against a cheap
on-disk signature. These tests drive the real tools and count the work.
"""

import os
import subprocess
from pathlib import Path

import pytest

import jev_engine
from scan_cache import ScanCache


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


@pytest.fixture
def workspace(tmp_path):
    for name in ("alpha", "beta", "gamma"):
        d = tmp_path / ".agents" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\ndescription: {name} helpers for the widget pipeline\n---\n# {name}\n",
            encoding="utf-8",
        )
    return tmp_path


@pytest.fixture
def git_workspace(workspace):
    """A real git repository, so the `git ls-files` fast path is the one taken."""
    src = workspace / "src"
    src.mkdir()
    (src / "core.py").write_text("def run(): pass", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q"], cwd=str(workspace), check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), check=True, env=env)
    return workspace


class TestScanCacheUnit:
    def test_hit_returns_the_cached_value(self):
        cache = ScanCache()
        cache.put("k", ("sig",), {"a": 1})
        assert cache.get("k", lambda: ("sig",)) == {"a": 1}

    def test_miss_when_signature_changes(self):
        cache = ScanCache()
        cache.put("k", ("sig",), "value")
        assert cache.get("k", lambda: ("other",)) is None

    def test_miss_when_absent(self):
        assert ScanCache().get("nope", lambda: ()) is None

    def test_ttl_expiry_with_a_frozen_clock(self, monkeypatch):
        import scan_cache

        now = [1000.0]
        monkeypatch.setattr(scan_cache.time, "monotonic", lambda: now[0])
        cache = ScanCache(ttl_s=5.0)
        cache.put("k", ("sig",), "value")
        now[0] += 6.0
        assert cache.get("k", lambda: ("sig",)) is None

    def test_ttl_slides_on_use(self, monkeypatch):
        import scan_cache

        now = [1000.0]
        monkeypatch.setattr(scan_cache.time, "monotonic", lambda: now[0])
        cache = ScanCache(ttl_s=5.0)
        cache.put("k", ("sig",), "value")
        for _ in range(4):
            now[0] += 4.0
            assert cache.get("k", lambda: ("sig",)) == "value"
        now[0] += 6.0
        assert cache.get("k", lambda: ("sig",)) is None

    def test_signature_errors_are_a_miss_not_a_crash(self):
        cache = ScanCache()
        cache.put("k", ("sig",), "value")

        def _boom():
            raise OSError("gone")

        assert cache.get("k", _boom) is None

    def test_concurrent_readers_see_one_consistent_value(self):
        cache = ScanCache()
        cache.put("k", ("sig",), list(range(500)))
        results = []
        errors = []

        def _reader():
            try:
                results.append(cache.get("k", lambda: ("sig",)))
            except Exception as err:  # pragma: no cover - failure detail
                errors.append(err)

        threads = [__import__("threading").Thread(target=_reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(results) == 10
        assert all(r == results[0] for r in results)


class TestMinimalScanDirs:
    def test_collapses_nested_defaults(self, workspace):
        dirs = [
            workspace / ".agents" / "skills",
            workspace / ".agents" / "workflows",
            workspace / ".agents" / "memory",
            workspace / ".agents",
        ]
        assert jev_engine._minimal_scan_dirs(dirs) == [workspace / ".agents"]

    def test_leaves_siblings_alone(self, workspace):
        (workspace / "docs" / "skills").mkdir(parents=True)
        (workspace / "docs").mkdir(exist_ok=True)
        (workspace / "skills").mkdir(exist_ok=True)
        dirs = [workspace / "docs", workspace / "docs" / "skills", workspace / "skills"]
        assert set(jev_engine._minimal_scan_dirs(dirs)) == {workspace / "docs", workspace / "skills"}

    def test_nested_docs_still_finds_its_files(self, workspace):
        # Collapsing must not change *which* files are discovered.
        (workspace / "docs" / "skills").mkdir(parents=True)
        (workspace / "docs" / "skills" / "guide.md").write_text("# guide", encoding="utf-8")
        found, truncated = jev_engine._discover_markdown(
            workspace, [workspace / "docs", workspace / "docs" / "skills"]
        )
        assert "docs/skills/guide.md" in found
        assert truncated is False

    def test_missing_dirs_are_dropped(self, workspace):
        dirs = [workspace / "nope", workspace / ".agents"]
        assert jev_engine._minimal_scan_dirs(dirs) == [workspace / ".agents"]


class TestMarkdownDiscoveryIsCached:
    def _count_walks(self, monkeypatch):
        calls = []
        real = Path.rglob

        def _spy(self, pattern):
            calls.append(str(self))
            return real(self, pattern)

        monkeypatch.setattr(Path, "rglob", _spy)
        return calls

    def test_second_identical_call_does_not_re_walk(self, workspace, monkeypatch):
        calls = self._count_walks(monkeypatch)
        jev_engine.find_agent_resources("widget pipeline", str(workspace))
        walked_after_first = len(calls)
        assert walked_after_first >= 1

        jev_engine.find_agent_resources("widget pipeline", str(workspace))
        assert len(calls) == walked_after_first, "the tree was re-walked on a warm call"

    def test_adding_a_skill_invalidates_the_cache(self, workspace, monkeypatch):
        calls = self._count_walks(monkeypatch)
        jev_engine.find_agent_resources("widget pipeline", str(workspace))
        before = len(calls)

        new_dir = workspace / ".agents" / "skills" / "delta"
        new_dir.mkdir(parents=True)
        (new_dir / "SKILL.md").write_text(
            "---\ndescription: delta widget pipeline helpers\n---\n# delta\n", encoding="utf-8"
        )
        result = jev_engine.find_agent_resources("delta widget pipeline", str(workspace))
        assert len(calls) > before, "a directory change did not invalidate the cache"
        assert ".agents/skills/delta/SKILL.md" in [
            r["file"] for r in result["ranked"]
        ]

    def test_cache_survives_a_caller_mutating_the_result(self, workspace):
        first = jev_engine.find_agent_resources("widget pipeline", str(workspace))
        first["resources"].append({"name": "bogus", "file": "x", "content": "y"})
        second = jev_engine.find_agent_resources("widget pipeline", str(workspace))
        assert all(r["file"] != "x" for r in second["resources"])

    def test_default_paths_walk_each_file_once(self, workspace, monkeypatch):
        seen = []
        real_rglob = Path.rglob

        def _spy(self, pattern):
            for item in real_rglob(self, pattern):
                seen.append(item)
                yield item

        monkeypatch.setattr(Path, "rglob", _spy)
        jev_engine.find_agent_resources("widget pipeline", str(workspace))
        # `.agents` and `.agents/skills` both listed by default; the nested one
        # must be dropped or every skill file is discovered twice.
        assert len(seen) == len(set(seen))


class TestFileDiscoveryIsCached:
    def test_second_call_does_not_fork_git_ls_files(self, git_workspace, monkeypatch):
        calls = []
        real_run = subprocess.run

        def _spy(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("args"))
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        jev_engine.select_target_files("edit alpha", str(git_workspace))
        forsked = len([c for c in calls if c and c[0] == "git" and "ls-files" in c])
        assert forsked == 1, "the git discovery path did not run at all"

        jev_engine.select_target_files("edit alpha", str(git_workspace))
        forsked_after = len([c for c in calls if c and c[0] == "git" and "ls-files" in c])
        assert forsked_after == forsked, "git ls-files was forked again on a warm call"

    def test_adding_a_file_invalidates_the_git_cache(self, git_workspace, monkeypatch):
        calls = []
        real_run = subprocess.run

        def _spy(*args, **kwargs):
            calls.append(1)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        jev_engine.select_target_files("edit alpha", str(git_workspace))
        (git_workspace / "src" / "new_module.py").write_text("y = 2", encoding="utf-8")
        result = jev_engine.select_target_files("edit new_module", str(git_workspace))
        assert len(calls) > 1, "a new file did not invalidate the git discovery cache"
        assert "src/new_module.py" in [r["file"] for r in result["ranked"]]

    def test_git_signature_never_forks_git(self, git_workspace, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("the signature must not shell out")

        monkeypatch.setattr(subprocess, "run", _boom)
        git_dir = jev_engine._git_dir(git_workspace)
        assert git_dir is not None
        signature = jev_engine._git_signature(git_workspace, git_dir)
        assert signature[1] and signature[1].startswith("ref:")
        assert signature[3] is not None   # index mtime

    def test_non_git_walk_is_cached(self, workspace, monkeypatch):
        (workspace / "src").mkdir()
        (workspace / "src" / "core.py").write_text("def run(): pass", encoding="utf-8")
        real_walk_files = jev_engine._walk_files
        calls = []

        def _spy(*args, **kwargs):
            calls.append(1)
            return real_walk_files(*args, **kwargs)

        monkeypatch.setattr(jev_engine, "_walk_files", _spy)
        jev_engine.select_target_files("edit core", str(workspace))
        assert len(calls) == 1
        jev_engine.select_target_files("edit core", str(workspace))
        assert len(calls) == 1, "the directory walk repeated on a warm call"

    def test_a_new_file_invalidates_the_walk_cache(self, workspace, monkeypatch):
        (workspace / "src").mkdir()
        (workspace / "src" / "core.py").write_text("def run(): pass", encoding="utf-8")
        real_walk_files = jev_engine._walk_files
        calls = []

        def _spy(*args, **kwargs):
            calls.append(1)
            return real_walk_files(*args, **kwargs)

        monkeypatch.setattr(jev_engine, "_walk_files", _spy)
        jev_engine.select_target_files("edit core", str(workspace))
        (workspace / "src" / "extra.py").write_text("x = 1", encoding="utf-8")
        jev_engine.select_target_files("edit core", str(workspace))
        assert len(calls) == 2, "a new file did not invalidate the cached walk"


class TestDiscoveryBounds:
    def test_max_discovered_files_overflow_is_reported(self, workspace, monkeypatch):
        skills = workspace / ".agents" / "skills" / "bulk"
        skills.mkdir(parents=True)
        for i in range(12):
            (skills / f"doc_{i:02d}.md").write_text("# doc", encoding="utf-8")
        monkeypatch.setattr(jev_engine, "MAX_DISCOVERED_FILES", 5)

        seen = {}

        def _spy(state, questions):
            seen["count"] = len(questions["primary"].criteria)
            from typesafe_sdk import ChoiceAnswer, SystemOneResponse, Usage
            return (
                SystemOneResponse(
                    model="jev-latest",
                    answers={
                        "primary": ChoiceAnswer(
                            choice="none", confidence=0.1,
                            probabilities={".agents/skills/bulk/doc_00.md": 0.05, "none": 0.95},
                        )
                    },
                    usage=Usage(input_tokens=5, output_tokens=2),
                ),
                {
                    "truncated": False,
                    "coverage": {
                        "complete": True,
                        "original_chars": 1,
                        "evaluated_chars": 1,
                        "estimated_tokens": {"state": 1, "questions": 1, "longest_question": 1},
                        "estimator": "chars/4",
                    },
                },
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _spy)
        result = jev_engine.find_agent_resources("doc", str(workspace))

        assert seen["count"] == 6, "the discovery cap did not bound the walk"
        assert result["candidates_considered"] == 5
        assert result["candidates_truncated"] is True
        assert "candidates_truncated" in result["reason_codes"]
        assert result["action"] != "auto"
        assert result["coverage"]["candidate_fields"]["complete"] is False


class TestWarmCacheDoesLessPerFileWork:
    def test_warm_call_never_stats_a_file(self, workspace, monkeypatch):
        stats = []
        real_is_file = Path.is_file

        def _spy(self):
            stats.append(str(self))
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", _spy)
        jev_engine.find_agent_resources("widget pipeline", str(workspace))
        cold = len(stats)
        assert cold >= 3, "the cold call was expected to stat every candidate"

        jev_engine.find_agent_resources("widget pipeline", str(workspace))
        assert len(stats) == cold, "the warm call re-walked the tree"

    def test_caller_cannot_mutate_the_cached_candidate_map(self, workspace):
        first, _ = jev_engine._discover_markdown(workspace, [workspace / ".agents"])
        first[".agents/injected.md"] = Path("nope")
        second, _ = jev_engine._discover_markdown(workspace, [workspace / ".agents"])
        assert ".agents/injected.md" not in second
