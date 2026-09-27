import os
from pathlib import Path

from typesafe_sdk import ChoiceAnswer, NoulAnswer, SystemOneResponse, Usage
import jev_engine
from config import ensure_dotenv
from jev_logging import _redact


class TestLogRedaction:
    """Redaction removes the credential and keeps the field around it."""

    def test_credential_is_removed_and_context_survives(self):
        out = _redact("curl -H 'Authorization: Bearer sk-abc123def456' https://example.test/x")
        assert "sk-abc123def456" not in out
        assert "<redacted>" in out
        assert "curl" in out and "https://example.test/x" in out

    def test_key_shaped_tokens_are_redacted(self):
        assert "sk-abcdef123456" not in _redact("sk-abcdef123456")
        assert "apikey_1234567890" not in _redact("apikey_1234567890")
        assert "SUPERSECRET" not in _redact("api_key=SUPERSECRET")
        assert "SUPERSECRET" not in _redact("api_key: SUPERSECRET")

    def test_case_insensitivity(self):
        assert "SK-UPPER-CASE" not in _redact("SK-UPPER-CASE")
        assert "TS_PROJECT_SECRET" not in _redact("TS_PROJECT_SECRET")
        assert "eyJhbGciOi" not in _redact("Bearer eyJhbGciOiJIUzI1NiJ9")
        assert "dXNlcjpwYXNz" not in _redact("AUTHORIZATION: dXNlcjpwYXNzd29yZA")

    def test_long_non_secrets_survive(self):
        # The removed heuristic redacted any 41-char alphanumeric run, which
        # threw away commit SHAs, hashes and long identifiers for no reason.
        long_token = "a" * 41
        assert _redact(long_token) == long_token

        commit_sha = "0123456789abcdef0123456789abcdef01234567"
        assert _redact(commit_sha) == commit_sha

        sentence = "This is a normal log message describing an operation that took place in workspace"
        assert _redact(sentence) == sentence

        filepath = "d:/mcp/jev-typesafe-mcp/subdir/another/file.txt"
        assert _redact(filepath) == filepath

        # A word that merely *ends* in a key prefix is not a key.
        assert _redact("disk-backup-options-configured") == "disk-backup-options-configured"

    def test_short_strings_untouched(self):
        assert _redact("hello") == "hello"
        assert _redact("status_ok") == "status_ok"

    def test_non_string_types(self):
        assert _redact(12345) == "12345"
        assert _redact(None) == "None"
        assert _redact(True) == "True"


class TestEnsureDotenv:
    """Tests for Task 8C: Idempotent dotenv loading."""

    def test_ensure_dotenv_does_not_override(self, monkeypatch):
        monkeypatch.delenv("JEV_MCP_MOCK", raising=False)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        import dotenv
        monkeypatch.setattr(
            dotenv, "dotenv_values", lambda **kwargs: {"JEV_MCP_MOCK": "1"}
        )
        # An authoritative injected value must survive: the MCP `environment`
        # block wins over the file, so a stale .env cannot flip the live judge
        # to the mock one.
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        res = ensure_dotenv()
        assert res is False
        assert os.environ["JEV_MCP_MOCK"] == "0"

    def test_ensure_dotenv_passes_env_path_when_exists(self, monkeypatch):
        called = {}

        def mock_dotenv_values(**kwargs):
            called.update(kwargs)
            return {}

        import dotenv
        monkeypatch.setattr(dotenv, "dotenv_values", mock_dotenv_values)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        ensure_dotenv()
        assert "dotenv_path" in called
        assert called["dotenv_path"].name == ".env"

    def test_ensure_dotenv_heals_blank_injected_value(self, monkeypatch):
        """A blank injected value must not shadow the .env.

        opencode resolves `"TYPESAFE_API_KEY": "{env:TYPESAFE_API_KEY}"` to an
        empty string when the variable is missing from its own environment, and
        `load_dotenv(override=False)` treats "already present" as authoritative
        even for `""` — so the file could never take effect and every live call
        failed with CONFIG_ERROR.
        """
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        import dotenv
        monkeypatch.setattr(
            dotenv, "dotenv_values", lambda **kwargs: {"TYPESAFE_API_KEY": "key-from-file"}
        )
        monkeypatch.setenv("TYPESAFE_API_KEY", "")
        assert ensure_dotenv() is True
        assert os.environ["TYPESAFE_API_KEY"] == "key-from-file"

    def test_ensure_dotenv_heals_whitespace_injected_value(self, monkeypatch):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        import dotenv
        monkeypatch.setattr(
            dotenv, "dotenv_values", lambda **kwargs: {"TYPESAFE_API_KEY": "key-from-file"}
        )
        monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
        assert ensure_dotenv() is True
        assert os.environ["TYPESAFE_API_KEY"] == "key-from-file"

    def test_ensure_dotenv_skips_when_missing(self, monkeypatch):
        called = {}

        def mock_dotenv_values(**kwargs):
            called.update(kwargs)
            return {}

        import dotenv
        monkeypatch.setattr(dotenv, "dotenv_values", mock_dotenv_values)
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
                        ),
                        "is_relevant": NoulAnswer(noul=0.9),
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
