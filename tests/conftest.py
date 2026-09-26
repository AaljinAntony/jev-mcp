import sys
import tempfile
from pathlib import Path

import pytest

# Make the repo root importable from tests/ (repo modules are not a package).
ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _grant_tempdir_as_allowed_root(monkeypatch):
    """Let `root_dir` point inside the system temp dir.

    `root_dir` is confined to an allowlist (CWD + ancestors +
    `JEV_MCP_ALLOWED_ROOTS`). pytest's `tmp_path` lives under the system temp
    dir, which is deliberately *not* on that list — a test asking about
    `tmp_path` is a legitimate request, not an escape. Granting the temp dir
    here keeps those tests meaningful instead of weakening the allowlist.
    """
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", tempfile.gettempdir())


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Clear the module-level config/client/settings/breaker caches around every test.

    `config.get_config`, `jev_engine.get_client`, `jev_engine.load_jev_settings`
    and the circuit breaker all memoize process-wide state. Without this, a test
    that changes the environment (e.g. `JEV_MCP_MOCK`) is silently ignored by
    whichever test ran after it and cached the previous environment.
    """
    import config
    import jev_engine

    def _clear():
        config._reset_config_cache()
        jev_engine._reset_client_cache()
        jev_engine._reset_settings_cache()
        jev_engine._reset_breaker()

    _clear()
    yield
    _clear()
