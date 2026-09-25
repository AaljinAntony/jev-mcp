# Phase 4: Client Caching, Settings Caching, and Error Logging

> **Phase**: 04  
> **Target Files**:
> - [`config.py`](file:///d:/mcp/jev-typesafe-mcp/config.py)
> - [`jev_engine.py`](file:///d:/mcp/jev-typesafe-mcp/jev_engine.py)
> - [`tests/test_mock_tools.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_mock_tools.py)  
> **Parallel Execution Track**:
> - In parallel mode:
>   - **Track B Agent**: Modifies `config.py`.
>   - **Track E Agent**: Modifies `jev_engine.py`.
> - In sequential mode: Single agent applies all changes.

---

## 1. Problem Context & Rationale

### Issue 1: `load_jev_settings()` Swallows Parse Errors to `sys.stderr`
When reading `jevs_settings.json` or `opencode.json`, if a JSON syntax error exists (e.g. trailing comma, typo):
```python
except Exception as e:
    sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
    continue
```
In standard MCP stdio mode, stderr is often discarded or unmonitored by the host. The server silently falls back to default settings with model routing disabled and built-in paths only, leaving the user with no structured clue why their settings stopped working.
**Fix**: Emit a structured `log_event("settings_parse_error", file=str(cfg), error=str(e))` to the persistent rotating log.

### Issue 2: `get_config()` Re-parsing Env Vars on Every Invocation
`get_config()` reads 6 environment variables, executes regex/string stripping, and parses floating point numbers on every tool call and sub-operation.
**Fix**: Cache the parsed `JevConfig` instance. Provide `_reset_config_cache()` so test suites can toggle `JEV_MCP_MOCK` or API keys cleanly.

### Issue 3: `load_jev_settings()` Repeated Filesystem I/O
Every skill search and resource check calls `load_jev_settings()`, which probes up to 6 filesystem locations.
**Fix**: Implement an mtime-aware cache. Cache the parsed settings along with the modification timestamps (`mtime`) of the discovered files. If the files have not changed, return the cached dictionary without re-reading the disk.

---

## 2. Step-by-Step Code Changes

### Step 2.1: `config.py` — Cache `get_config()`

**Target lines** (`config.py` lines 79–91):
Add module-level cache and reset function:

```python
_cached_config: Optional[JevConfig] = None


def _reset_config_cache() -> None:
    """Clear the cached config. Exposed for tests."""
    global _cached_config
    _cached_config = None


def get_config() -> JevConfig:
    """Parse + validate the environment into a cached `JevConfig`."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    config = JevConfig(
        api_key=(os.getenv("TYPESAFE_API_KEY") or "").strip(),
        model=(os.getenv("JEV_MCP_MODEL") or DEFAULT_MODEL).strip(),
        mock=_bool_env("JEV_MCP_MOCK"),
        auto_accept=_num_env("JEV_MCP_AUTO_ACCEPT", 0.8),
        review_at=_num_env("JEV_MCP_REVIEW_AT", 0.5),
        timeout_ms=_timeout_env(),
    )
    if config.review_at > config.auto_accept:
        raise JevConfigError("JEV_MCP_REVIEW_AT must not exceed JEV_MCP_AUTO_ACCEPT.")

    _cached_config = config
    return config
```
*Note: Make sure `from typing import Optional` is present at the top of `config.py`.*

---

### Step 2.2: `jev_engine.py` — Settings Logging & Mtime Caching

#### 1. Import `log_event`:
Ensure `log_event` is imported from `jev_logging`:
```python
from jev_logging import log_round, log_event
```

#### 2. Implement Settings Cache with Mtime and Structured Logging:
**Target lines** (`jev_engine.py` lines 136–169):
```python
_cached_settings: Optional[dict] = None
_cached_settings_mtimes: dict = {}


def _reset_settings_cache() -> None:
    """Reset the settings cache. Exposed for tests."""
    global _cached_settings, _cached_settings_mtimes
    _cached_settings = None
    _cached_settings_mtimes = {}


def _get_files_mtime_signature(files: List[Path]) -> dict:
    """Return a mapping of file path -> mtime for cache invalidation."""
    mtimes = {}
    for f in files:
        try:
            mtimes[str(f)] = f.stat().st_mtime
        except (OSError, FileNotFoundError):
            mtimes[str(f)] = None
    return mtimes


def load_jev_settings() -> dict:
    """Read Jev settings from jevs_settings.json (project wins over user) with mtime caching."""
    global _cached_settings, _cached_settings_mtimes

    candidates = _find_settings_files() + _find_config_files()
    current_mtimes = _get_files_mtime_signature(candidates)

    if _cached_settings is not None and _cached_settings_mtimes == current_mtimes:
        return _cached_settings

    settings = {
        "enable_model_routing": False,
        "models": {},
        "scan_paths": list(DEFAULT_SCAN_PATHS),
        "source": None,
    }

    # 1. Try jevs_settings.json candidates
    for cfg in _find_settings_files():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            log_event("settings_parse_error", file=str(cfg), error=str(e))
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if _apply_jev_settings(settings, raw.get("jev_settings") or raw):
            settings["source"] = str(cfg)
            _cached_settings = settings
            _cached_settings_mtimes = current_mtimes
            return settings

    # 2. Try opencode.json candidates
    for cfg in _find_config_files():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            log_event("settings_parse_error", file=str(cfg), error=str(e))
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if _apply_jev_settings(settings, raw.get("jev_settings") or {}):
            settings["source"] = str(cfg)
            _cached_settings = settings
            _cached_settings_mtimes = current_mtimes
            return settings

    _cached_settings = settings
    _cached_settings_mtimes = current_mtimes
    return settings
```

---

### Step 2.3: `tests/conftest.py` & Test Suite Updates
Ensure test fixtures reset caches before tests run:
```python
# In tests/conftest.py or test setup
from config import _reset_config_cache
from jev_engine import _reset_settings_cache, _reset_client_cache

@pytest.fixture(autouse=True)
def reset_caches():
    _reset_config_cache()
    _reset_settings_cache()
    _reset_client_cache()
    yield
    _reset_config_cache()
    _reset_settings_cache()
    _reset_client_cache()
```

---

## 3. Verification Commands

Run tests to ensure config and settings caching behave properly:
```powershell
.venv\Scripts\pytest tests/test_mock_tools.py -v
```

Expected output:
- All tools execute properly.
- Cache resets avoid test pollution across environment changes.

---

## 4. Acceptance Criteria
- [ ] `config.get_config()` caches its result and exposes `_reset_config_cache()`.
- [ ] `jev_engine.load_jev_settings()` uses mtime validation to skip disk I/O when unchanged.
- [ ] `log_event("settings_parse_error", ...)` is called on corrupted JSON settings files.
- [ ] Cache invalidation works seamlessly in the test suite.
