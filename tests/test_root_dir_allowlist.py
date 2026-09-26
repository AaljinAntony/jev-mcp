"""`root_dir` must be confined to an allowlist, not screened by a denylist.

The old check named five POSIX paths and two Windows environment variables, so
`/home/<user>`, `/var`, `/proc`, `C:\\Users\\<user>` and `C:\\ProgramData` were all
reachable from an LLM-supplied argument — a prompt-injected agent could pass
`root_dir="C:\\Users\\aalji"` and get a file listing plus file contents.

The invariant now: a supplied `root_dir` carries no more privilege than the
session's own working directory. The CWD, its ancestors, and anything under
`JEV_MCP_ALLOWED_ROOTS` are allowed; everything else is rejected.
"""

import os
import tempfile
from pathlib import Path

import pytest

import jev_engine
from config import _reset_config_cache, get_config
from jev_errors import JevValidationError


def _reject(path) -> str:
    with pytest.raises(JevValidationError) as exc:
        jev_engine._validate_root_dir(str(path))
    return str(exc.value)


# ----------------------------------------------------------------------
# Allowed
# ----------------------------------------------------------------------
def test_repo_root_is_allowed():
    assert jev_engine._validate_root_dir(".") == Path.cwd().resolve()


def test_subdirectory_of_the_cwd_is_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg" / "inner").mkdir(parents=True)
    assert jev_engine._validate_root_dir(str(tmp_path / "pkg" / "inner")) == (
        tmp_path / "pkg" / "inner"
    ).resolve()


def test_ancestor_of_the_cwd_is_allowed(tmp_path, monkeypatch):
    """Hosts launch the server with cwd=project root, or with a temp dir."""
    (tmp_path / "deep" / "deeper").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "deep" / "deeper")
    assert jev_engine._validate_root_dir(str(tmp_path)) == tmp_path.resolve()


def test_explicit_allowed_root_is_accepted(tmp_path, monkeypatch):
    # Point the allowlist somewhere unrelated to the CWD.
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "elsewhere" / "project"
    outside.mkdir(parents=True)
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", str(outside))
    _reset_config_cache()
    assert jev_engine._validate_root_dir(str(outside)) == outside.resolve()


def test_several_allowed_roots_are_pathsep_separated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", os.pathsep.join([str(a), str(b)]))
    _reset_config_cache()
    assert get_config().allowed_roots == (str(a.resolve()), str(b.resolve()))
    assert jev_engine._validate_root_dir(str(b)) == b.resolve()


# ----------------------------------------------------------------------
# Rejected
# ----------------------------------------------------------------------
def test_user_home_is_rejected():
    msg = _reject(Path.home())
    assert "outside the allowed roots" in msg or "system directory" in msg


def test_parent_of_home_is_rejected():
    """`C:\\Users` was the headline gap in the old denylist."""
    _reject(Path.home().parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows layout")
def test_program_data_is_rejected():
    for candidate in (Path(os.environ.get("ProgramData", "C:/ProgramData")), Path("C:/ProgramData")):
        if candidate.exists():
            _reject(candidate)
            return


@pytest.mark.skipif(os.name == "nt", reason="POSIX layout")
@pytest.mark.parametrize("path", ["/home", "/var", "/etc", "/proc", "/sys", "/dev", "/opt", "/srv", "/root"])
def test_posix_system_trees_are_rejected(path):
    msg = _reject(path)
    assert "outside the allowed roots" in msg or "system directory" in msg


def test_volume_roots_are_rejected():
    if os.name == "nt":
        for drive in ("C:\\", "D:\\", "E:\\", "Z:\\"):
            _reject(drive)
    else:
        msg = _reject("/")
        assert "system directory" in msg


def test_sibling_with_a_shared_prefix_is_rejected(tmp_path, monkeypatch):
    """`<root>-evil` must not pass a naive `startswith` check.

    Built outside the CWD tree on purpose: a sibling of the CWD is always
    covered by the "CWD's ancestors are allowed" rule (its parent is one), so
    it cannot demonstrate the prefix bug. Here only the *explicitly configured*
    root admits the path, and its textual twin must still be refused.
    """
    allowed = tmp_path / "roots" / "project"
    evil = tmp_path / "roots" / "project-evil"
    allowed.mkdir(parents=True)
    evil.mkdir()
    # The CWD stays put (the repo root), so neither `allowed` nor `evil` is the
    # CWD, a descendant of it, or an ancestor of it. Only the explicitly
    # configured root can admit `allowed`.
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", str(allowed))
    _reset_config_cache()

    assert jev_engine._validate_root_dir(str(allowed)) == allowed.resolve()
    assert str(evil).startswith(str(allowed))          # what `startswith` would allow
    assert "outside the allowed roots" in _reject(evil)
    assert "outside the allowed roots" in _reject(tmp_path / "roots")


def test_symlinked_escape_is_rejected(tmp_path, monkeypatch):
    """The case a `startswith` check on the *unresolved* path would miss."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "private.txt").write_text("do not read me", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", str(workspace))
    _reset_config_cache()

    link = workspace / "escape"
    try:
        link.symlink_to(secret, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("cannot create directory symlinks in this environment")

    # Path.resolve() follows the link, so the resolved path is `secret`, which is
    # neither the workspace nor under it.
    assert "outside the allowed roots" in _reject(link)
    # A naive `str(link).startswith(str(workspace))` would have said yes.
    assert str(link).startswith(str(workspace))


def test_nonexistent_allowed_root_entry_is_ignored_not_fatal(tmp_path, monkeypatch):
    missing = tmp_path / "roots" / "does_not_exist_abc"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", str(missing))
    _reset_config_cache()
    # Parsing must not raise...
    assert get_config().allowed_roots == (str(missing.resolve()),)
    # ...and the absent directory must not be offered as an allowed root.
    assert missing not in jev_engine._allowed_roots()


def test_relative_allowed_root_entry_is_dropped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", "relative/path")
    _reset_config_cache()
    assert get_config().allowed_roots == ()


def test_conftest_grants_the_temp_dir_so_pytest_tmp_path_still_works(tmp_path):
    """The suite's own `tmp_path` must remain a legitimate `root_dir`."""
    assert jev_engine._validate_root_dir(str(tmp_path)) == tmp_path.resolve()
    assert tempfile.gettempdir().lower() in str(tmp_path).lower()


def test_missing_directory_still_reports_the_specific_message(tmp_path):
    msg = _reject(tmp_path / "nope_abc123")
    assert "does not exist or is not a directory" in msg


def test_a_file_is_not_a_root(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    assert "does not exist or is not a directory" in _reject(f)


def test_system_dir_message_survives_the_allowlist_gate(monkeypatch):
    """`C:\\Windows` must keep its specific wording, not the generic one."""
    if os.name != "nt":
        pytest.skip("Windows only")
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", os.environ.get("SystemRoot", "C:/Windows"))
    _reset_config_cache()
    assert "system directory" in _reject(os.environ.get("SystemRoot", "C:/Windows"))


def test_is_within_uses_relative_to_not_startswith():
    base = Path("C:/work/project")
    assert jev_engine._is_within(Path("C:/work/project/pkg"), base) is True
    assert jev_engine._is_within(base, base) is True
    assert jev_engine._is_within(Path("C:/work/project-evil"), base) is False
    assert jev_engine._is_within(Path("C:/work"), base) is False
