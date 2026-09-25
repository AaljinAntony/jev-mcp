# Phase 4: Client Caching + Settings Fix (Reliability)

> **Priority:** 🟡 High
> **Estimated effort:** ~1 hour
> **Files to modify:** `jev_engine.py`, `config.py`

---

## Task 4A: Cache `TypeSafeClient` for Connection Reuse

### Problem

`get_client()` in `jev_engine.py` (lines 143–154) creates a **new** `TypeSafeClient` on every tool call. Each new client:
- Opens a fresh HTTP connection pool
- Performs a full TLS handshake (~100–300ms)
- Allocates a new `httpx2` transport

Since the MCP server is a long-lived process handling many tool calls, reusing a single client saves significant latency.

### Where to look

- File: `jev_engine.py`, lines 143–154
- The `TypeSafeClient` constructor takes `api_key`, `timeout`, and `retry`

### Exact fix

Replace the `get_client()` function with a cached version. The cache must invalidate when the configuration changes (important for tests that toggle `JEV_MCP_MOCK`).

```python
# Module-level cache
_cached_client: Optional[TypeSafeClient] = None
_cached_client_key: Optional[tuple] = None


def get_client() -> Optional[TypeSafeClient]:
    """Return a configured TypeSafe client, or None in mock mode.

    Caches the client and reuses it across calls as long as the
    configuration (api_key, model, mock, timeout) hasn't changed.
    Raises `JevConfigError` when `TYPESAFE_API_KEY` is missing (unless
    `JEV_MCP_MOCK=1`, which never needs a key).
    """
    global _cached_client, _cached_client_key

    cfg = get_config()
    if cfg.mock:
        _cached_client = None
        _cached_client_key = None
        return None
    if not cfg.api_key:
        raise JevConfigError("TYPESAFE_API_KEY environment variable is not configured.")

    # Cache key: invalidate when any client-relevant config changes
    cache_key = (cfg.api_key, cfg.timeout_ms, cfg.model)
    if _cached_client is not None and _cached_client_key == cache_key:
        return _cached_client

    _cached_client = TypeSafeClient(
        api_key=cfg.api_key,
        timeout=cfg.timeout_ms / 1000.0,
        retry=RetryPolicy(
            max_retries=2,
            backoff_initial=0.5,
            backoff_max=5.0,
            backoff_jitter=0.25,
        ),
    )
    _cached_client_key = cache_key
    return _cached_client
```

### Important: Test compatibility

The test suite uses `monkeypatch.setenv("JEV_MCP_MOCK", "1")` which makes `get_config().mock` return `True`, causing `get_client()` to return `None` and clear the cache. This is correct — mock mode should never use a cached real client.

However, tests that monkeypatch `execute_system_one` directly (like `test_mock_tools.py`) bypass `get_client()` entirely, so the cache doesn't affect them.

Add a module-level function to explicitly clear the cache (useful for tests):

```python
def _reset_client_cache() -> None:
    """Clear the cached client. Exposed for tests."""
    global _cached_client, _cached_client_key
    _cached_client = None
    _cached_client_key = None
```

### What NOT to change

- Do NOT make the client a frozen module-level singleton — it must be possible to reconfigure.
- Do NOT cache the `JevConfig` object itself — the docstring on `config.py` explicitly says it re-reads env on every call for test flexibility.

---

## Task 4B: Fix CWD-Dependent Settings Lookup

### Problem

`load_jev_settings()` in `jev_engine.py` (lines 95–127) uses `Path.cwd()` (via `_find_settings_files()` and `_find_config_files()`) to locate settings files. When the MCP server is started, the host application (opencode, Cursor, Claude Desktop, etc.) sets the working directory — this may or may not be the user's project root.

This means:
- `jevs_settings.json` in the project root might not be found
- `opencode.json` in the project root might not be found
- Settings fall back to defaults silently

### Where to look

- File: `jev_engine.py`, lines 52–76 (`_find_settings_files()`, `_find_config_files()`)
- File: `jev_engine.py`, lines 95–127 (`load_jev_settings()`)
- File: `jev_engine.py`, lines 130–140 (`get_scan_paths()`)

### Exact fix

Add the **script directory** (the directory containing `jev_engine.py`) as a search location. This is always the repo root, regardless of `cwd`.

**Modify `_find_settings_files()`:**

```python
def _find_settings_files() -> List[Path]:
    """Locate jevs_settings.json candidates: project-level first, user-level last."""
    candidates = []
    # 1. Check cwd (for when invoked from the project root)
    for name in ("jevs_settings.json", ".opencode/jevs_settings.json"):
        local = Path.cwd() / name
        if local.exists():
            candidates.append(local)
    # 2. Check script directory (the repo root, cwd-independent)
    script_dir = Path(__file__).resolve().parent
    if script_dir != Path.cwd().resolve():
        for name in ("jevs_settings.json", ".opencode/jevs_settings.json"):
            script_local = script_dir / name
            if script_local.exists() and script_local not in candidates:
                candidates.append(script_local)
    # 3. User-level config
    user = Path.home() / ".config" / "opencode" / "jevs_settings.json"
    if user.exists():
        candidates.append(user)
    return candidates
```

**Apply the same pattern to `_find_config_files()`:**

```python
def _find_config_files() -> List[Path]:
    """Locate opencode.json candidates: project-level first, user-level last."""
    candidates = []
    for name in ("opencode.json", ".opencode/opencode.json"):
        local = Path.cwd() / name
        if local.exists():
            candidates.append(local)
    script_dir = Path(__file__).resolve().parent
    if script_dir != Path.cwd().resolve():
        for name in ("opencode.json", ".opencode/opencode.json"):
            script_local = script_dir / name
            if script_local.exists() and script_local not in candidates:
                candidates.append(script_local)
    user = Path.home() / ".config" / "opencode" / "opencode.json"
    if user.exists():
        candidates.append(user)
    return candidates
```

### Design decisions

- **Priority order preserved:** `cwd` still wins over script dir, which wins over user-level config.
- **Deduplication:** The `not in candidates` check prevents the same file from appearing twice when `cwd == script_dir`.
- **Backward-compatible:** If someone already relies on `cwd`-relative settings, they still work.

---

## Checklist

- [x] `jev_engine.py`: Replace `get_client()` with cached version (including `_cached_client`, `_cached_client_key`, and `_reset_client_cache()`)
- [x] `jev_engine.py`: Update `_find_settings_files()` to include script directory
- [x] `jev_engine.py`: Update `_find_config_files()` to include script directory
- [x] Run `pytest tests/ -v` — all tests green
- [x] Manual test: start the MCP server from a different directory and verify it still finds `jevs_settings.json`
