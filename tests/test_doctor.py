"""Tests for `scripts/doctor.py`'s OpenCode MCP config check.

The check exists because the failure it looks for is *silent*: a config that does
not match the installed opencode's schema, or a command path that was never
substituted, makes the server disappear from the tool list rather than raise. A
check that only ever prints `[ok]` would be worthless, so most of these are
negative cases.

`_mcp_config_files` and `_opencode_major` are monkeypatched rather than driven
through the real filesystem and PATH: the config search depends on the CWD and on
`Path.home()`, which is `USERPROFILE` on Windows and `HOME` on POSIX, and a test
that has to know which is which tests the platform instead of the check.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import doctor  # noqa: E402


VALID_V1 = {
    "mcp": {
        "jev-engine": {
            "type": "local",
            "enabled": True,
            "command": ["C:/repo/.venv/Scripts/python.exe", "C:/repo/jev_mcp.py"],
        }
    }
}

VALID_V2 = {
    "mcp": {
        "servers": {
            "jev-engine": {
                "type": "local",
                "disabled": False,
                "codemode": False,
                "command": ["C:/repo/.venv/Scripts/python.exe", "C:/repo/jev_mcp.py"],
            }
        }
    }
}


@pytest.fixture(autouse=True)
def _reset_counters():
    """`doctor` accumulates failures/warnings in module globals."""
    doctor._FAILURES = 0
    doctor._WARNINGS = 0
    yield
    doctor._FAILURES = 0
    doctor._WARNINGS = 0


@pytest.fixture
def run_check(tmp_path, monkeypatch, capsys):
    """Run `check_mcp_config` over one config dict; return the printed report.

    `existing` lists the `command` entries that exist on disk, so a config can be
    checked without creating a real checkout.
    """

    def _run(config, *, major=1, existing=("C:/repo/.venv/Scripts/python.exe", "C:/repo/jev_mcp.py"),
             bom=False, name="opencode.json"):
        path = tmp_path / name
        text = json.dumps(config)
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))

        monkeypatch.setattr(doctor, "_mcp_config_files", lambda: [path])
        monkeypatch.setattr(doctor, "_opencode_major", lambda: major)
        # Only the paths we declare as existing resolve; anything else is missing.
        # Compared with separators normalized, because `str(Path("C:/a/b"))` comes
        # back as `C:\a\b` on Windows and the fixture must not be platform-specific.
        wanted = {p.replace("\\", "/") for p in existing}
        real_exists = Path.exists

        def _exists(self):
            return str(self).replace("\\", "/") in wanted or real_exists(self)

        monkeypatch.setattr(Path, "exists", _exists)
        doctor.check_mcp_config()
        return capsys.readouterr().out

    return _run


class TestServerEntry:
    def test_reads_the_v1_shape(self):
        entry, shape = doctor._server_entry(VALID_V1, "jev-engine")
        assert shape == "v1"
        assert entry["type"] == "local"

    def test_reads_the_v2_shape(self):
        entry, shape = doctor._server_entry(VALID_V2, "jev-engine")
        assert shape == "v2"
        assert entry["codemode"] is False

    def test_absent_server_is_not_an_error_here(self):
        assert doctor._server_entry({"mcp": {"other": {}}}, "jev-engine") == (None, None)
        assert doctor._server_entry({}, "jev-engine") == (None, None)
        assert doctor._server_entry({"mcp": {"servers": {}}}, "jev-engine") == (None, None)

    def test_v1_wins_when_both_shapes_are_present(self):
        """Project precedence is the caller's job; this just must not crash."""
        merged = {"mcp": {"jev-engine": {"type": "local"}, "servers": {"jev-engine": {"type": "remote"}}}}
        entry, shape = doctor._server_entry(merged, "jev-engine")
        assert (entry["type"], shape) == ("local", "v1")


class TestValidConfigs:
    def test_valid_v1_config_reports_no_problems(self, run_check):
        out = run_check(VALID_V1, major=1)
        assert "[XX]" not in out
        assert "[ok] 'jev-engine' configured" in out
        assert doctor._FAILURES == 0

    def test_valid_v2_config_reports_no_problems(self, run_check):
        out = run_check(VALID_V2, major=2)
        assert "[XX]" not in out
        assert "(V2 shape)" in out
        assert doctor._FAILURES == 0

    def test_environment_block_is_optional_and_says_so(self, run_check):
        out = run_check(VALID_V1, major=1)
        assert "falls back to its documented default" in out
        assert "[XX] environment block" not in out

    def test_environment_block_is_summarised_when_present(self, run_check):
        config = json.loads(json.dumps(VALID_V1))
        config["mcp"]["jev-engine"]["environment"] = {"JEV_MCP_MOCK": "0", "JEV_MCP_TIMEOUT_MS": "30000"}
        out = run_check(config, major=1)
        assert "2 variable(s)" in out


class TestPlaceholderIsCaught:
    """The exact bug: README's copy step ships the example unsubstituted."""

    def test_unsubstituted_repo_dir_placeholder_fails(self, run_check):
        config = json.loads(json.dumps(VALID_V1))
        config["mcp"]["jev-engine"]["command"] = [
            "<REPO_DIR>/.venv/Scripts/python.exe",
            "<REPO_DIR>/jev_mcp.py",
        ]
        out = run_check(config, major=1, existing=())
        assert "[XX] command paths substituted" in out
        assert "<REPO_DIR>/jev_mcp.py" in out
        assert "[XX] command paths exist" in out
        assert doctor._FAILURES >= 2

    def test_the_shipped_example_is_flagged(self, run_check):
        """Guards the example against regressing back to a broken install."""
        example = json.loads((ROOT / "config" / "opencode.example.json").read_text(encoding="utf-8"))
        out = run_check(example, major=1, existing=())
        assert "[XX] command paths substituted" in out
        assert doctor._FAILURES >= 1

    def test_a_real_path_is_not_mistaken_for_a_placeholder(self, run_check):
        out = run_check(VALID_V1, major=1)
        assert "[XX] command paths substituted" not in out


class TestShapeMismatch:
    def test_v2_config_on_a_1x_host_fails(self, run_check):
        out = run_check(VALID_V2, major=1)
        assert "[XX] config shape matches the installed opencode" in out
        assert "flatten it" in out
        assert doctor._FAILURES == 1

    def test_v1_config_on_a_2x_host_fails(self, run_check):
        out = run_check(VALID_V1, major=2)
        assert "[XX] config shape matches the installed opencode" in out
        assert "nest it" in out

    def test_undetectable_version_does_not_invent_a_mismatch(self, run_check):
        """A host that is not opencode must not be told its config is wrong."""
        out = run_check(VALID_V2, major=None)
        assert "[XX] config shape matches the installed opencode" not in out
        assert "[!!] opencode version" in out
        assert "(V2 shape)" in out


class TestEnableKey:
    def test_enabled_under_v2_fails(self, run_check):
        config = json.loads(json.dumps(VALID_V2))
        config["mcp"]["servers"]["jev-engine"].pop("disabled")
        config["mcp"]["servers"]["jev-engine"]["enabled"] = True
        out = run_check(config, major=2)
        assert "[XX] enable key" in out
        assert "disabled" in out

    def test_disabled_under_v1_fails(self, run_check):
        config = json.loads(json.dumps(VALID_V1))
        config["mcp"]["jev-engine"].pop("enabled")
        config["mcp"]["jev-engine"]["disabled"] = True
        out = run_check(config, major=1)
        assert "[XX] enable key" in out
        assert "enabled" in out

    def test_absent_enable_key_is_fine(self, run_check):
        config = json.loads(json.dumps(VALID_V1))
        config["mcp"]["jev-engine"].pop("enabled")
        out = run_check(config, major=1)
        assert "connects by default" in out
        assert doctor._FAILURES == 0


class TestCodemode:
    def test_v2_without_codemode_warns(self, run_check):
        config = json.loads(json.dumps(VALID_V2))
        config["mcp"]["servers"]["jev-engine"].pop("codemode")
        out = run_check(config, major=2)
        assert "[!!] codemode" in out
        assert "Code Mode" in out
        assert doctor._FAILURES == 0  # a warning, not a failure

    def test_v2_with_codemode_false_is_quiet(self, run_check):
        out = run_check(VALID_V2, major=2)
        assert "native tool list" in out
        assert "[!!] codemode" not in out


class TestStructuralFailures:
    def test_missing_command_fails(self, run_check):
        config = json.loads(json.dumps(VALID_V1))
        config["mcp"]["jev-engine"].pop("command")
        out = run_check(config, major=1)
        assert "[XX] command" in out

    def test_wrong_type_fails(self, run_check):
        config = json.loads(json.dumps(VALID_V1))
        config["mcp"]["jev-engine"]["type"] = "remote"
        out = run_check(config, major=1)
        assert "[XX] type" in out

    def test_string_command_warns_rather_than_fails(self, run_check):
        config = json.loads(json.dumps(VALID_V1))
        config["mcp"]["jev-engine"]["command"] = "C:/repo/jev_mcp.py"
        out = run_check(config, major=1)
        assert "[!!] command shape" in out
        assert doctor._FAILURES == 0

    def test_a_bom_does_not_hide_the_config(self, run_check):
        """Windows PowerShell writes one; it must not read as a parse failure."""
        out = run_check(VALID_V1, major=1, bom=True)
        assert "parses" not in out or "[XX] opencode.json parses" not in out
        assert "[ok] 'jev-engine' configured" in out
        assert doctor._FAILURES == 0

    def test_malformed_json_is_reported_not_raised(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "opencode.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(doctor, "_mcp_config_files", lambda: [path])
        monkeypatch.setattr(doctor, "_opencode_major", lambda: 1)
        doctor.check_mcp_config()
        out = capsys.readouterr().out
        assert "[XX] opencode.json parses" in out

    def test_absent_server_falls_through_to_the_next_file(self, tmp_path, monkeypatch, capsys):
        """A project config without the server must not shadow the global one."""
        project = tmp_path / "opencode.json"
        project.write_text(json.dumps({"mcp": {"audacity": {"type": "local"}}}), encoding="utf-8")
        other = tmp_path / "global.json"
        other.write_text(json.dumps(VALID_V1), encoding="utf-8")
        monkeypatch.setattr(doctor, "_mcp_config_files", lambda: [project, other])
        monkeypatch.setattr(doctor, "_opencode_major", lambda: 1)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        doctor.check_mcp_config()
        out = capsys.readouterr().out
        assert "global.json" in out
        assert "[XX]" not in out
