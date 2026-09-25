import sys
from pathlib import Path

import pytest

# Make the repo root importable from tests/ (repo modules are not a package).
ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Clear the module-level config/client/settings caches around every test.

    `config.get_config`, `jev_engine.get_client` and `jev_engine.load_jev_settings`
    all memoize process-wide state. Without this, a test that changes the
    environment (e.g. `JEV_MCP_MOCK`) is silently ignored by whichever test ran
    after it and cached the previous environment.
    """
    import config
    import jev_engine

    def _clear():
        config._reset_config_cache()
        jev_engine._reset_client_cache()
        jev_engine._reset_settings_cache()

    _clear()
    yield
    _clear()
