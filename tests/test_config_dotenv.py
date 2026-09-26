"""Tests for config.ensure_dotenv fallback behavior (override=False, no upward search)."""

import os
from pathlib import Path

from config import ensure_dotenv


class TestDotenvFallback:
    def test_ensure_dotenv_preserves_injected_env(self, monkeypatch, tmp_path):
        """Authoritative injected env variables must NEVER be overwritten by .env."""
        # Create a dummy .env in tmp_path
        env_file = tmp_path / ".env"
        env_file.write_text(
            "TYPESAFE_API_KEY=from-file\nJEV_MCP_MOCK=1\nNEW_FALLBACK_VAR=fallback-val\n",
            encoding="utf-8",
        )

        # Set authoritative injected environment
        monkeypatch.setenv("TYPESAFE_API_KEY", "from-injected-env")
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        monkeypatch.delenv("NEW_FALLBACK_VAR", raising=False)

        # Point ensure_dotenv to tmp_path / ".env"
        fake_parent = tmp_path
        import config
        monkeypatch.setattr(config, "Path", lambda *args: fake_parent / ".env" if args and args[0] == "__file__" else Path(*args))
        # More robust: monkeypatch Path(__file__).resolve().parent
        class PatchedPath(type(Path())):
            def __new__(cls, *args, **kwargs):
                return super().__new__(cls, *args, **kwargs)

        # Monkeypatch Path in config directly
        original_path = Path
        def mock_path(*args, **kwargs):
            p = original_path(*args, **kwargs)
            if args and "config.py" in str(args[0]):
                class FakeParent:
                    def resolve(self):
                        return self
                    @property
                    def parent(self):
                        return tmp_path
                return FakeParent()
            return p

        monkeypatch.setattr(config, "Path", mock_path)

        loaded = ensure_dotenv()
        assert loaded is True
        # Injected values MUST be preserved
        assert os.environ.get("TYPESAFE_API_KEY") == "from-injected-env"
        assert os.environ.get("JEV_MCP_MOCK") == "0"
        # Missing values fall back to .env
        assert os.environ.get("NEW_FALLBACK_VAR") == "fallback-val"

    def test_ensure_dotenv_skips_upward_cwd_search(self, monkeypatch, tmp_path):
        """Never search upward from an unrelated working directory."""
        import config
        # Create an .env in tmp_path (current working directory)
        cwd_env = tmp_path / ".env"
        cwd_env.write_text("UNRELATED_VAR=from-cwd\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        # But repo dir has NO .env
        repo_dir = tmp_path / "repo_without_env"
        repo_dir.mkdir()

        original_path = Path
        def mock_path(*args, **kwargs):
            p = original_path(*args, **kwargs)
            if args and "config.py" in str(args[0]):
                class FakeParent:
                    def resolve(self):
                        return self
                    @property
                    def parent(self):
                        return repo_dir
                return FakeParent()
            return p

        monkeypatch.setattr(config, "Path", mock_path)
        monkeypatch.delenv("UNRELATED_VAR", raising=False)

        loaded = ensure_dotenv()
        assert loaded is False
        assert "UNRELATED_VAR" not in os.environ
