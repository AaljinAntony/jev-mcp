"""`_dir_signature`: the cache's invalidation probe.

These tests pin the signature's *contract*, not its speed. It is the thing that
decides whether a cached discovery result is still true, so the properties worth
protecting are: it is cheap and bounded, it changes when the tree changes, and it
changes **immediately** — no reuse window.

That last one is a rejected optimisation, documented here because it is the kind
of change that looks free. Reusing a signature for a second measured as 5.4 ms
saved on a 13.8 ms mock-warm call, which reads as a 40% win. A *live* call is
388 ms, so it was 1.4% of wall clock — and it broke the contract below. The
existing invalidation tests in `test_scan_cache.py` caught it; this file keeps the
property named so the next attempt has to argue with it rather than rediscover it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jev_engine  # noqa: E402


@pytest.fixture
def tree(tmp_path):
    """A scan tree with a few nested directories."""
    for name in ("alpha", "beta", "gamma"):
        d = tmp_path / ".agents" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\ndescription: {name}\n---\n# {name}\n", encoding="utf-8")
    return tmp_path


class TestSignatureShape:
    def test_a_signature_is_a_sorted_tuple_of_directory_mtimes(self, tree):
        sig = jev_engine._dir_signature([tree / ".agents"])
        assert sig, "expected at least the root directory"
        assert all(isinstance(entry, tuple) and len(entry) == 2 for entry in sig)
        assert all(isinstance(mtime, int) or mtime is None for _, mtime in sig)
        assert all(isinstance(path, str) for path, _ in sig)
        assert list(sig) == sorted(sig), "sorted, so two equal trees compare equal"

    def test_it_is_deterministic(self, tree):
        assert jev_engine._dir_signature([tree / ".agents"]) == jev_engine._dir_signature(
            [tree / ".agents"]
        )

    def test_an_unreadable_directory_is_recorded_not_raised(self, tmp_path):
        """A directory that vanished mid-walk is a `None` mtime, not an exception:
        a scan must not fail because a tree changed under it."""
        missing = tmp_path / "not-there"
        assert jev_engine._dir_signature([missing]) == ((str(missing), None),)

    def test_ignore_dirs_are_excluded(self, tree):
        noisy = tree / ".agents" / "node_modules"
        noisy.mkdir()
        (noisy / "junk.md").write_text("x", encoding="utf-8")
        kept = jev_engine._dir_signature([tree / ".agents"])
        ignored = jev_engine._dir_signature([tree / ".agents"], ignore_dirs={"node_modules"})
        assert any("node_modules" in p for p, _ in kept)
        assert not any("node_modules" in p for p, _ in ignored)


class TestSignatureIsBounded:
    """It has to be cheaper than the walk it guards."""

    def test_it_descends_to_the_level_limit_and_no_further(self, tree):
        """Depth 3 is tracked; depth 4 is the first thing dropped.

        `.agents` is depth 0, `skills` depth 1, a skill depth 2, and `x` below it
        depth 3 — all inside `SIG_MAX_LEVELS`. `y` at depth 4 is deliberately not
        tracked, which is what keeps this a bounded probe rather than a second full
        walk.
        """
        (tree / ".agents" / "skills" / "alpha" / "x" / "y").mkdir(parents=True)
        paths = [p for p, _ in jev_engine._dir_signature([tree / ".agents"])]
        assert any(p.endswith("alpha") for p in paths), "depth 2 must be reached"
        assert any(p.endswith("x") for p in paths), "depth 3 is within SIG_MAX_LEVELS"
        assert not any(p.endswith("y") for p in paths), "depth 4 must not be tracked"

    def test_it_never_exceeds_the_entry_cap(self, tree):
        assert len(jev_engine._dir_signature([tree / ".agents"], max_entries=2)) <= 2

    def test_a_deep_tree_does_not_blow_the_stack_or_the_budget(self, tmp_path):
        """A pathological depth must still terminate at the cap."""
        deep = tmp_path / ".agents"
        for i in range(30):
            deep = deep / f"level{i}"
            deep.mkdir(parents=True)
        sig = jev_engine._dir_signature([tmp_path / ".agents"])
        assert 0 < len(sig) <= jev_engine.SIG_MAX_ENTRIES


class TestSignatureSeesChangeImmediately:
    """The property that rules out a reuse window.

    A memo here would save 5.4 ms of a 388 ms live call and cost this. The test is
    written to fail loudly the moment someone reintroduces a TTL, rather than
    leaving it to be discovered as a stale candidate list in the wild.
    """

    def test_a_new_directory_changes_the_signature_on_the_next_call(self, tree):
        before = jev_engine._dir_signature([tree / ".agents"])
        fresh = tree / ".agents" / "skills" / "delta"
        fresh.mkdir()
        (fresh / "SKILL.md").write_text("---\ndescription: delta\n---\n", encoding="utf-8")

        after = jev_engine._dir_signature([tree / ".agents"])
        assert after != before, "a new directory must invalidate immediately"
        assert any("delta" in p for p, _ in after)

    def test_a_removed_directory_changes_the_signature_on_the_next_call(self, tree):
        before = jev_engine._dir_signature([tree / ".agents"])
        (tree / ".agents" / "skills" / "beta" / "SKILL.md").unlink()
        (tree / ".agents" / "skills" / "beta").rmdir()

        after = jev_engine._dir_signature([tree / ".agents"])
        assert after != before
        assert not any(p.endswith("beta") for p, _ in after)

    def test_a_new_skill_is_discovered_without_any_reset(self, tree, monkeypatch):
        """End to end through the real scanner and cache: no TTL, no reset, no sleep."""
        monkeypatch.setenv("JEV_MCP_MOCK", "1")
        jev_engine._reset_scan_cache()
        found, _ = jev_engine._discover_markdown(tree, [tree / ".agents"])
        assert len(found) == 3

        fresh = tree / ".agents" / "skills" / "delta"
        fresh.mkdir()
        (fresh / "SKILL.md").write_text("---\ndescription: delta\n---\n", encoding="utf-8")

        found, _ = jev_engine._discover_markdown(tree, [tree / ".agents"])
        assert ".agents/skills/delta/SKILL.md" in found, (
            "the very next call must see the new skill"
        )

    def test_there_is_no_reuse_window_to_configure(self):
        """The rejected optimisation left no knob behind.

        If a future change wants one, it has to reintroduce the constant *and* deal
        with the contract above, which is the point.
        """
        assert not hasattr(jev_engine, "SIG_REUSE_S")
        assert not hasattr(jev_engine, "_sig_memo")