"""User and project `jev_settings` must merge per key, not first-wins.

This repo ships a `jevs_settings.json` whose keys are all at their defaults.
Under first-file-wins that file permanently shadowed
`~/.config/opencode/jevs_settings.json`, so turning routing on in the user-level
file silently did nothing — while the docs said "project wins over user", which
reads as intentional rather than as a trap.
"""

import json

import pytest

import jev_engine
from jev_engine import (
    DEFAULT_SCAN_PATHS,
    _reset_settings_cache,
    _snapshot,
    load_jev_settings,
)


def _write(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A CWD holding the project settings file, with no user-level file."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(jev_engine, "_find_settings_files", lambda: [project_dir / "jevs_settings.json"])
    monkeypatch.setattr(jev_engine, "_find_config_files", lambda: [])
    _reset_settings_cache()
    return project_dir


def test_user_models_survive_a_project_file_that_only_sets_routing(project, monkeypatch):
    project_file = project / "jevs_settings.json"
    _write(project_file, {"enable_model_routing": True})

    # The user-level file is discovered after the project one; the merge applies
    # it first even though discovery order is unchanged.
    user_file = project / "user.json"
    _write(user_file, {"models": {"fast": "provider/fast"}})
    monkeypatch.setattr(
        jev_engine, "_find_settings_files", lambda: [project_file, user_file]
    )
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["enable_model_routing"] is True
    assert settings["models"] == {"fast": "provider/fast"}


def test_empty_string_clears_an_inherited_tier(project, monkeypatch):
    project_file = project / "jevs_settings.json"
    user_file = project / "user.json"
    _write(user_file, {"models": {"fast": "a/f", "balanced": "a/b"}})
    _write(project_file, {"models": {"fast": "", "frontier": "a/x"}})
    monkeypatch.setattr(jev_engine, "_find_settings_files", lambda: [project_file, user_file])
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["models"] == {"balanced": "a/b", "frontier": "a/x"}


def test_project_routing_false_overrides_user_routing_true(project, monkeypatch):
    project_file = project / "jevs_settings.json"
    user_file = project / "user.json"
    _write(user_file, {"enable_model_routing": True, "models": {"fast": "a/f"}})
    _write(project_file, {"enable_model_routing": False})
    monkeypatch.setattr(jev_engine, "_find_settings_files", lambda: [project_file, user_file])
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["enable_model_routing"] is False
    assert settings["models"] == {"fast": "a/f"}


def test_scan_paths_union_across_files_and_keep_the_defaults(project, monkeypatch):
    project_file = project / "jevs_settings.json"
    user_file = project / "user.json"
    _write(user_file, {"scan_paths": ["custom-a", "shared"]})
    _write(project_file, {"scan_paths": ["custom-b", "shared"]})
    monkeypatch.setattr(jev_engine, "_find_settings_files", lambda: [project_file, user_file])
    _reset_settings_cache()

    paths = load_jev_settings()["scan_paths"]
    assert paths[: len(DEFAULT_SCAN_PATHS)] == list(DEFAULT_SCAN_PATHS)
    assert "custom-a" in paths
    assert "custom-b" in paths
    assert paths.count("shared") == 1


def test_a_malformed_project_file_does_not_block_the_user_file(project, monkeypatch):
    project_file = project / "jevs_settings.json"
    project_file.write_text("{ unquoted: invalid }", encoding="utf-8")
    user_file = project / "user.json"
    _write(user_file, {"enable_model_routing": True, "models": {"fast": "a/f"}})
    monkeypatch.setattr(jev_engine, "_find_settings_files", lambda: [project_file, user_file])
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["enable_model_routing"] is True
    assert settings["models"] == {"fast": "a/f"}
    assert settings["source"] == str(user_file)


def test_sources_lists_everything_and_source_is_the_last_contributor(project, monkeypatch):
    project_file = project / "jevs_settings.json"
    user_file = project / "user.json"
    _write(user_file, {"models": {"fast": "a/f"}})
    _write(project_file, {"enable_model_routing": True})
    monkeypatch.setattr(jev_engine, "_find_settings_files", lambda: [project_file, user_file])
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["sources"] == [str(user_file), str(project_file)]
    assert settings["source"] == str(project_file)


def test_opencode_json_is_only_read_through_its_jev_settings_block(project, monkeypatch):
    """Never take unrelated top-level keys from a full opencode.json."""
    project_file = project / "opencode.json"
    _write(
        project_file,
        {
            "$schema": "https://opencode.ai/config.json",
            "model": "some-provider/some-model",
            "models": {"fast": "should-be-ignored"},
            "mcp": {"jev": {"command": "python"}},
        },
    )
    monkeypatch.setattr(jev_engine, "_find_config_files", lambda: [project_file])
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["models"] == {}
    assert settings["enable_model_routing"] is False


def test_opencode_jev_settings_block_is_merged(project, monkeypatch):
    project_file = project / "opencode.json"
    _write(project_file, {"jev_settings": {"models": {"fast": "a/f"}, "enable_model_routing": True}})
    monkeypatch.setattr(jev_engine, "_find_config_files", lambda: [project_file])
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["models"] == {"fast": "a/f"}
    assert settings["enable_model_routing"] is True


def test_an_edit_after_a_load_is_picked_up(project):
    import os

    project_file = project / "jevs_settings.json"
    _write(project_file, {"enable_model_routing": False})
    _reset_settings_cache()
    assert load_jev_settings()["enable_model_routing"] is False

    _write(project_file, {"enable_model_routing": True})
    # The cache is mtime-keyed, so make the new mtime unambiguous rather than
    # depending on filesystem timestamp resolution.
    stat = project_file.stat()
    os.utime(project_file, (stat.st_atime + 10, stat.st_mtime + 10))
    assert load_jev_settings()["enable_model_routing"] is True


def test_mtimes_are_restatted_after_reading(project, monkeypatch):
    """A file edited between the signature and the read must not be cached stale.

    The old code stored the *pre-read* signature, so an edit landing in that
    window produced fresh content paired with a stale mtime — and the change
    was then never seen. Hard to hit deterministically, so assert the seam:
    the signature stored in the cache is the one taken *after* the read.
    """
    project_file = project / "jevs_settings.json"
    _write(project_file, {"enable_model_routing": True})

    calls = {"n": 0}

    def _sig(files):
        calls["n"] += 1
        return {"before": 1} if calls["n"] == 1 else {"after": 2}

    monkeypatch.setattr(jev_engine, "_get_files_mtime_signature", _sig)
    _reset_settings_cache()
    load_jev_settings()

    assert jev_engine._cached_settings_mtimes == {"after": 2}


# ----------------------------------------------------------------------
# #20: the cache must not be reachable through what callers get back
# ----------------------------------------------------------------------
def test_two_loads_are_equal_but_distinct_objects(project):
    _reset_settings_cache()
    s1 = load_jev_settings()
    s2 = load_jev_settings()
    assert s1 == s2
    assert s1 is not s2
    assert s1["models"] is not s2["models"]
    assert s1["scan_paths"] is not s2["scan_paths"]


def test_mutating_a_returned_models_map_does_not_corrupt_the_cache(project, monkeypatch):
    _write(project / "jevs_settings.json", {"models": {"fast": "a/f"}})
    _reset_settings_cache()

    first = load_jev_settings()
    first["models"]["fast"] = "hijacked"
    first["models"]["injected"] = "nope"
    first["enable_model_routing"] = True

    second = load_jev_settings()
    assert second["models"] == {"fast": "a/f"}
    assert second["enable_model_routing"] is False


def test_mutating_a_returned_scan_paths_list_does_not_affect_the_next_load(project, monkeypatch):
    _write(project / "jevs_settings.json", {"scan_paths": ["extra"]})
    _reset_settings_cache()

    first = load_jev_settings()
    first["scan_paths"].append("injected")
    first["scan_paths"].clear()

    second = load_jev_settings()
    assert "extra" in second["scan_paths"]
    assert len(second["scan_paths"]) >= len(DEFAULT_SCAN_PATHS)


def test_snapshot_copies_nested_containers():
    original = {"models": {"a": 1}, "scan_paths": ["x"], "sources": ["s"], "source": "s"}
    snap = _snapshot(original)
    assert snap == original
    assert snap["models"] is not original["models"]
    assert snap["scan_paths"] is not original["scan_paths"]
    assert snap["sources"] is not original["sources"]
    snap["models"]["a"] = 2
    assert original["models"]["a"] == 1
