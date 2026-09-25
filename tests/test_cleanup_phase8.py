import os
from pathlib import Path
import pytest
from unittest.mock import patch

from typesafe_sdk import ChoiceAnswer, SystemOneResponse, Usage
import jev_engine
from config import ensure_dotenv
from jev_logging import _redact, _SECRET_MARKERS


class TestLogRedaction:
    """Tests for Task 8B: Improved log redaction."""

    def test_marker_redaction(self):
        # All secret markers should trigger redaction
        assert _redact("sk-abcdef123456") == "<redacted>"
        assert _redact("ts_secretkey999") == "<redacted>"
        assert _redact("apikey_12345") == "<redacted>"
        assert _redact("api_key=mysecret") == "<redacted>"
        assert _redact("api_key:mysecret") == "<redacted>"
        assert _redact("typesafe_api_key_test") == "<redacted>"
        assert _redact("Bearer eyJhbGciOi...") == "<redacted>"
        assert _redact("Authorization: Bearer foo") == "<redacted>"

    def test_case_insensitivity(self):
        assert _redact("SK-Upper-Case") == "<redacted>"
        assert _redact("TS_PROJECT_SECRET") == "<redacted>"
        assert _redact("BEARER token123") == "<redacted>"
        assert _redact("AUTHORIZATION: basic xxx") == "<redacted>"

    def test_heuristic_long_alphanumeric_keys(self):
        # > 40 chars alphanumeric (allowing - and _)
        key_41 = "a" * 41
        assert _redact(key_41) == "<redacted>"

        key_with_hyphens = "abcde-12345-fghij-67890-klmno-12345-pqrst-67890"
        assert len(key_with_hyphens) > 40
        assert _redact(key_with_hyphens) == "<redacted>"

        key_with_underscores = "sec_12345_67890_abcde_fghij_klmno_pqrst_uvwxyz_99"
        assert len(key_with_underscores) > 40
        assert _redact(key_with_underscores) == "<redacted>"

    def test_heuristic_does_not_redact_non_secrets(self):
        # 40 chars exactly (e.g., git commit SHA) should not be redacted by heuristic
        commit_sha = "0123456789abcdef0123456789abcdef01234567"
        assert len(commit_sha) == 40
        assert _redact(commit_sha) == commit_sha

        # Sentences > 40 chars with spaces
        sentence = "This is a normal log message describing an operation that took place in workspace"
        assert len(sentence) > 40
        assert _redact(sentence) == sentence

        # File paths > 40 chars with slashes and periods
        filepath = "d:/mcp/jev-typesafe-mcp/subdir/another/file.txt"
        assert len(filepath) > 40
        assert _redact(filepath) == filepath

        # Short strings
        assert _redact("hello") == "hello"
        assert _redact("status_ok") == "status_ok"

    def test_non_string_types(self):
        assert _redact(12345) == "12345"
        assert _redact(None) == "None"
        assert _redact(True) == "True"


class TestEnsureDotenv:
    """Tests for Task 8C: Idempotent dotenv loading."""

    def test_ensure_dotenv_does_not_override(self, monkeypatch):
        called = {}

        def mock_load_dotenv(**kwargs):
            called.update(kwargs)
            return True

        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv", mock_load_dotenv)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        res = ensure_dotenv()
        assert res is True
        assert called.get("override") is False

    def test_ensure_dotenv_passes_env_path_when_exists(self, monkeypatch):
        called = {}

        def mock_load_dotenv(**kwargs):
            called.update(kwargs)
            return True

        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv", mock_load_dotenv)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        ensure_dotenv()
        assert called.get("override") is False
        assert "dotenv_path" in called
        assert called["dotenv_path"].name == ".env"

    def test_ensure_dotenv_skips_when_missing(self, monkeypatch):
        called = {}

        def mock_load_dotenv(**kwargs):
            called.update(kwargs)
            return True

        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv", mock_load_dotenv)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        res = ensure_dotenv()
        assert res is False
        assert called == {}

    def test_ensure_dotenv_handles_import_error(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "dotenv":
                raise ImportError("No module named 'dotenv'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        # Should not raise, returns False
        assert ensure_dotenv() is False


class TestSymlinkProtection:
    """Tests for Task 8A: Symlink protection in select_target_files."""

    def test_symlink_skipped_in_select_target_files_rglob(self, tmp_path, monkeypatch):
        # Force non-git fallback path by ensuring _discover_files_git returns None
        monkeypatch.setattr(jev_engine, "_discover_files_git", lambda *a, **kw: None)

        file1 = tmp_path / "valid1.py"
        file1.write_text("print('hello')", encoding="utf-8")

        symlink_candidate = tmp_path / "symlinked.py"
        symlink_candidate.write_text("print('symlink')", encoding="utf-8")

        # Mock Path.is_symlink so symlink_candidate is identified as a symlink
        original_is_symlink = Path.is_symlink

        def fake_is_symlink(self):
            if self.name == "symlinked.py":
                return True
            return original_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

        # Mock _request in jev_engine to inspect candidate options passed to Choice
        captured_choices = []

        def mock_request(prompt, questions):
            captured_choices.extend(questions["target_file"].criteria.keys())
            return (
                SystemOneResponse(
                    model="jev-latest",
                    answers={
                        "target_file": ChoiceAnswer(
                            choice="valid1.py",
                            confidence=0.9,
                            probabilities={"valid1.py": 0.9, "none": 0.1},
                        )
                    },
                    usage=Usage(input_tokens=10, output_tokens=5),
                ),
                {"truncated": False, "coverage": 1.0},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", mock_request)

        res = jev_engine.select_target_files("find file", str(tmp_path))
        assert "valid1.py" in captured_choices
        assert "symlinked.py" not in captured_choices
        assert res["matched"] is True
        assert res["files"] == ["valid1.py"]
