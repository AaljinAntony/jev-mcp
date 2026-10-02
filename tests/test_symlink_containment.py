"""A candidate's path can be inside the workspace while its content is not.

Every other confinement here works on the *string* an LLM supplied: `root_dir`
and `task_file` are checked against an allowlist and both refuse anything outside
it. Discovery breaks that model. A repository can track a symlink pointing
anywhere on the machine, and `Path.is_file()` follows it, so a path that passes
every textual check still resolves to `~/.ssh/id_rsa` — and those bytes then
become Choice criteria and are sent to the provider.

The invariant is therefore enforced where paths are produced, not where they are
read: **discovery never yields a link** (`candidates.is_link`). There are three
discovery paths and all three must agree, so most of these tests are about that
agreement rather than about any one of them.

Why not gate the reader instead: resolving each candidate to compare it against
the root measured ~30 ms per call at 118 candidates on Windows — 38% of the
whole `search_agent_skills` request — against ~3 ms for `is_symlink` paid once
per cached walk. See the note on `candidates.is_link`.

Symlink creation needs elevation or Developer Mode on Windows, so the tests that
want a real link skip there. The `TestGatesRunWithoutSymlinkSupport` cases
simulate one instead, so the branch is still executed on every platform; CI's
`ubuntu-latest` runs the real thing.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import candidates  # noqa: E402
import jev_engine  # noqa: E402

SECRET = "PRIVATE-KEY-MATERIAL-NOT-FOR-THE-PROVIDER"


def _symlink(link: Path, target: Path) -> bool:
    """Create a symlink, or report that this host will not let us."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError):
        return False
    return True


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, timeout=30, check=True
    )


@pytest.fixture
def escape(tmp_path):
    """A repo, a secret outside it, and a tracked symlink from one to the other."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text(SECRET, encoding="utf-8")
    if not _symlink(root / "escape.md", outside):
        pytest.skip("this host forbids symlink creation (Windows needs elevation or Developer Mode)")
    (root / "real.md").write_text("# a real file\n", encoding="utf-8")
    return root, "escape.md"


class TestIsLink:
    def test_an_ordinary_file_is_not_a_link(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text("x", encoding="utf-8")
        assert candidates.is_link(f) is False

    def test_a_stat_error_counts_as_a_link(self, tmp_path, monkeypatch):
        """Undecidable is treated as unsafe: refusing a candidate is recoverable,
        offering one leaks its content. Reachable from Python 3.13, where
        `is_symlink` propagates OSError instead of swallowing it."""
        def _boom(self):
            raise OSError("cannot stat")

        monkeypatch.setattr(Path, "is_symlink", _boom)
        assert candidates.is_link(tmp_path / "anything.md") is True


class TestGitDiscovery:
    def test_a_tracked_symlink_is_not_offered(self, escape):
        root, link = escape
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        found = jev_engine._discover_files_git(root, set(), 250, set())
        assert link not in (found or [])
        assert "real.md" in (found or [])

    def test_end_to_end_discovery_offers_no_link(self, escape):
        root, link = escape
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        found = jev_engine._discover_candidates(root, set(), 250, {".git"})
        assert link not in found
        assert "real.md" in found


class TestMarkdownDiscovery:
    """The path that feeds `search_agent_skills`, and the one `rglob` leaks."""

    def test_a_linked_markdown_file_is_not_discovered(self, escape):
        root, link = escape
        found, _ = jev_engine._discover_markdown(root, [root])
        assert link not in found
        assert "real.md" in found

    def test_the_cache_does_not_smuggle_a_link_back(self, escape):
        """The scan cache stores the result; a link must not enter it either."""
        root, link = escape
        jev_engine._reset_scan_cache()
        first, _ = jev_engine._discover_markdown(root, [root])
        jev_engine._reset_scan_cache()
        second, _ = jev_engine._discover_markdown(root, [root])
        assert link not in first and link not in second

    def test_the_skills_reader_never_sees_the_secret(self, escape):
        root, link = escape
        found, _ = jev_engine._discover_markdown(root, [root])
        _, read = candidates.read_text_cache(root)
        for name in found:
            assert SECRET not in read(name)


class TestAllThreePathsAgree:
    """The invariant, stated once.

    The walk path already had coverage before this file
    (`test_cleanup_phase8.py::test_symlink_skipped_in_select_target_files_rglob`,
    which mocks `Path.is_symlink` the same way). What is new here is the *git*
    path, which had none, the file-symlink case under `rglob`, and the agreement
    itself: three discovery paths that each refuse links on their own will drift
    unless something asserts all three at once.
    """

    def test_no_discovery_path_yields_a_link(self, escape):
        root, link = escape
        _git(root, "init", "-q")
        _git(root, "add", "-A")

        via_git = jev_engine._discover_files_git(root, set(), 250, set()) or []
        via_walk = jev_engine._walk_files(root, set(), set(), 250, 5)
        via_rglob, _ = jev_engine._discover_markdown(root, [root])

        assert link not in via_git
        assert link not in via_walk
        assert link not in via_rglob
        # And the real file is found by all three, so none of them is just
        # refusing everything.
        for found in (via_git, via_walk, via_rglob):
            assert "real.md" in found


class TestGatesRunWithoutSymlinkSupport:
    """Simulated links, so the branch is executed where a real one cannot be made."""

    @pytest.fixture
    def pretend_link(self, monkeypatch):
        """Make `docs/escape.md` behave like a symlink to outside the repo."""
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: self.name == "escape.md" or real_is_symlink(self),
        )

    @pytest.fixture
    def repo_with_escape(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "escape.md").write_text(SECRET, encoding="utf-8")
        (root / "real.md").write_text("# a real file\n", encoding="utf-8")
        return root

    def test_git_discovery_drops_it(self, repo_with_escape, pretend_link):
        root = repo_with_escape
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        found = jev_engine._discover_files_git(root, set(), 250, set())
        assert "escape.md" not in found, "the gate did not drop the link"
        assert "real.md" in found, "the gate dropped a real file too"

    def test_rglob_discovery_drops_it(self, repo_with_escape, pretend_link):
        found, _ = jev_engine._discover_markdown(repo_with_escape, [repo_with_escape])
        assert "escape.md" not in found
        assert "real.md" in found

    def test_the_caller_never_receives_the_secret(self, repo_with_escape, pretend_link):
        root = repo_with_escape
        jev_engine._reset_scan_cache()
        found, _ = jev_engine._discover_markdown(root, [root])
        _, read = candidates.read_text_cache(root)
        assert all(SECRET not in read(name) for name in found)


class TestAllowlistUnchanged:
    def test_containment_uses_relative_to_not_startswith(self):
        base = Path("C:/work/project")
        assert jev_engine._is_within(Path("C:/work/project-evil"), base) is False
        assert jev_engine._is_within(Path("C:/work/project/pkg"), base) is True

    def test_a_refused_candidate_would_still_held_its_option(self):
        """Discovery removing a candidate must not remove its Choice option.

        A Choice cannot pick a value it was never given, so the criteria builder
        has to keep the key. Nothing in `build_criteria` may prune.
        """
        criteria = candidates.build_criteria(["a.md", "b.md"], lambda p: "")
        assert set(criteria) == {"a.md", "b.md"}
        assert criteria["a.md"] == candidates.UNREADABLE
