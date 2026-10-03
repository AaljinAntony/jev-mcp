"""Globally installed skills must be discoverable next to the workspace's own.

Discovery used to key every candidate relative to `root_dir` and silently drop
anything that did not fit, so `~/.agents/skills` was invisible: the only way to
read it was to point the whole call at it with `root_dir`, which dropped the
workspace's skills in exchange. The scan dirs are now home-derived rather than
configured, so a machine that keeps its global skills somewhere else still needs
no repository edit.
"""

import os

import pytest

import candidates
import jev_engine

#: `conftest` replaces this with a stub for every other test module, so the whole
#: suite stays independent of the skills installed on the machine running it.
_REAL_GLOBAL_SCAN_DIRS = jev_engine._global_scan_dirs


@pytest.fixture(autouse=True)
def _real_global_scan(monkeypatch):
    monkeypatch.setattr(jev_engine, "_global_scan_dirs", _REAL_GLOBAL_SCAN_DIRS)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point `~` at an empty temp directory, and return it.

    `os.path.expanduser("~")` reads `USERPROFILE` on Windows and `HOME` on
    POSIX; both are set so the fixture behaves the same either way. Redirecting
    home is what makes these tests independent of the skills the machine
    running them happens to have installed.
    """
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.setenv("HOME", str(fake))
    return fake


@pytest.fixture(autouse=True)
def _no_inherited_global_scan_paths(monkeypatch):
    """`JEV_MCP_GLOBAL_SCAN_PATHS` is opt-in per test, never inherited from the shell."""
    monkeypatch.delenv("JEV_MCP_GLOBAL_SCAN_PATHS", raising=False)


def _skill(root, name, body):
    d = root / ".agents" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\ndescription: {name} helpers\n---\n{body}\n", encoding="utf-8"
    )
    return d / "SKILL.md"


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_home_relative_skill_is_found_and_shortened(home, tmp_path):
    """The reported symptom: an installed global skill is simply not there."""
    _skill(home, "global-one", "GLOBAL-UNIQUE-BODY")
    ws = _workspace(tmp_path)

    found, _ = jev_engine._discover_markdown(ws, jev_engine._global_scan_dirs())

    assert "~/.agents/skills/global-one/SKILL.md" in found


def test_global_skill_comes_back_with_its_content(home, tmp_path, stub_choice):
    """`~/`-shortened is a display key: the bytes behind it must still resolve."""
    _skill(home, "global-one", "GLOBAL-UNIQUE-BODY")
    ws = _workspace(tmp_path)
    key = "~/.agents/skills/global-one/SKILL.md"
    stub_choice(key, {key: 0.9, "none": 0.1})

    res = jev_engine.find_agent_resources("global one helpers", str(ws))

    assert res["matched"] is True
    assert res["primary"]["file"] == key
    assert "GLOBAL-UNIQUE-BODY" in res["resources"][0]["content"]


def test_global_dir_outside_home_falls_back_to_an_absolute_key(
    home, tmp_path, monkeypatch
):
    """Nothing to shorten against, so the key is absolute — and still readable."""
    elsewhere = tmp_path / "shared-skills"
    _skill(elsewhere, "shared-one", "SHARED-UNIQUE-BODY")
    ws = _workspace(tmp_path)
    monkeypatch.setenv("JEV_MCP_GLOBAL_SCAN_PATHS", str(elsewhere))

    found, _ = jev_engine._discover_markdown(ws, jev_engine._global_scan_dirs())

    key = (elsewhere / ".agents" / "skills" / "shared-one" / "SKILL.md").as_posix()
    assert key in found

    _, read = candidates.read_text_cache(ws)
    assert "SHARED-UNIQUE-BODY" in read(key)


def test_workspace_and_global_skills_are_offered_together(home, tmp_path):
    ws = _workspace(tmp_path)
    _skill(ws, "local-one", "LOCAL-UNIQUE-BODY")
    _skill(home, "global-one", "GLOBAL-UNIQUE-BODY")

    # The order the tool assembles them in: workspace first, globals appended.
    search_dirs = jev_engine.get_scan_paths(ws)
    seen = {str(p).lower() for p in search_dirs}
    search_dirs += [
        p for p in jev_engine._global_scan_dirs() if str(p).lower() not in seen
    ]
    found, _ = jev_engine._discover_markdown(ws, search_dirs)

    assert ".agents/skills/local-one/SKILL.md" in found
    assert "~/.agents/skills/global-one/SKILL.md" in found


def test_a_machine_without_global_skills_scans_exactly_as_before(home, tmp_path):
    """No global folders exist here, so the scan set is the workspace's alone."""
    ws = _workspace(tmp_path)

    assert jev_engine._global_scan_dirs() == []


def test_missing_configured_global_dir_is_inert(home, tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    monkeypatch.setenv("JEV_MCP_GLOBAL_SCAN_PATHS", str(tmp_path / "not-installed"))

    assert jev_engine._global_scan_dirs() == []


def test_several_configured_global_dirs_are_pathsep_separated(
    home, tmp_path, monkeypatch
):
    a, b = tmp_path / "a-skills", tmp_path / "b-skills"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("JEV_MCP_GLOBAL_SCAN_PATHS", os.pathsep.join([str(a), str(b)]))

    assert jev_engine._global_scan_dirs() == [a, b]


class TestReadTextCacheKeys:
    def test_tilde_key_expands(self, home, tmp_path):
        _skill(home, "global-one", "EXPANDED-BODY")
        _, read = candidates.read_text_cache(tmp_path / "ws")

        assert "EXPANDED-BODY" in read("~/.agents/skills/global-one/SKILL.md")

    def test_absolute_key_is_not_prefixed_with_root(self, tmp_path):
        skill = _skill(tmp_path / "elsewhere", "shared-one", "ABSOLUTE-BODY")
        root = tmp_path / "ws"
        root.mkdir(exist_ok=True)
        _, read = candidates.read_text_cache(root)

        assert "ABSOLUTE-BODY" in read(skill.as_posix())

    def test_relative_key_still_resolves_against_root(self, tmp_path):
        root = tmp_path / "ws"
        _skill(root, "local-one", "RELATIVE-BODY")
        _, read = candidates.read_text_cache(root)

        assert "RELATIVE-BODY" in read(".agents/skills/local-one/SKILL.md")