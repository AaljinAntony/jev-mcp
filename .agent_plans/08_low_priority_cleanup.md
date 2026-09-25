# Phase 8: Low-Priority Cleanup

> **Priority:** 🟢 Low
> **Estimated effort:** ~30 minutes
> **Files to modify:** `jev_engine.py`, `jev_logging.py`

---

## Task 8A: Add Symlink Protection to `select_target_files()` Directory Walk

### Problem

`select_target_files()` in `jev_engine.py` (line 529) uses `root.rglob("*")` which follows symlinks by default. A symlink loop (e.g. `ln -s . loop`) would cause infinite iteration and hang the MCP tool call.

### Where to look

- File: `jev_engine.py`, line 529

### Exact fix

Add a symlink filter after the `rglob`:

```python
candidates = []
for p in root.rglob("*"):
    if any(ignored in p.parts for ignored in ignore_dirs):
        continue
    if p.is_symlink():
        continue  # skip symlinks to prevent loops
    if p.is_file() and p.suffix.lower() not in ignore_exts:
        candidates.append(p.relative_to(root).as_posix())
    if len(candidates) >= MAX_CHOICE_OPTIONS:
        break
```

> **Note:** If Phase 5 (Task 5B) adds `git ls-files`, this only applies to the fallback `rglob` path. Git handles symlinks correctly.

---

## Task 8B: Improve Log Redaction

### Problem

`jev_logging.py` (lines 33–42) uses a fixed set of secret markers:
```python
_SECRET_MARKERS = ("sk-", "apikey_", "api_key=", "typesafe_api_key")
```

This misses:
- Keys with `ts_` prefix (potential future format)
- Bearer tokens in error messages
- Any key that doesn't start with `sk-`

### Where to look

- File: `jev_logging.py`, lines 33–42

### Exact fix

Expand the markers and add a length-based heuristic:

```python
#: Values that look like credentials and must never be written to the log.
_SECRET_MARKERS = (
    "sk-",
    "ts_",
    "apikey_",
    "api_key=",
    "api_key:",
    "typesafe_api_key",
    "bearer ",
    "authorization:",
)


def _redact(value) -> str:
    text = str(value)
    lowered = text.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            return "<redacted>"
    # Heuristic: long alphanumeric strings that look like API keys
    if len(text) > 40 and text.replace("-", "").replace("_", "").isalnum():
        return "<redacted>"
    return text
```

### Design decisions

- The 40-char heuristic catches most API keys (TypeSafe keys are ~90 chars) without false-positiving on normal log content.
- Adding `"bearer "` and `"authorization:"` catches HTTP auth headers that might appear in error messages.

---

## Task 8C: Clean Up Duplicate `dotenv` Loading

### Problem

Both `jev_mcp.py` (lines 7–15) and `jev_engine.py` (lines 8–17) have identical `dotenv` loading blocks. When `jev_mcp.py` imports `jev_engine`, the `.env` file is loaded twice (harmless but wasteful and a maintenance risk if one is changed without the other).

### Exact fix

Remove the `dotenv` block from `jev_engine.py` (lines 8–17). The entry points are:
1. `jev_mcp.py` (MCP server) — loads `.env` before importing `jev_engine`
2. `jev_engine.py` `__main__` (CLI) — needs its own loading

Cleaner approach: move the dotenv loading into a shared function in `config.py` and call it from both entry points:

```python
# config.py — add at the top:
def ensure_dotenv():
    """Load .env if python-dotenv is available. Idempotent."""
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
        else:
            load_dotenv(override=True)
    except ImportError:
        pass
```

Then in both `jev_mcp.py` and `jev_engine.py`'s `__main__` block:
```python
from config import ensure_dotenv
ensure_dotenv()
```

Remove the inline try/except dotenv blocks from both files.

---

## Checklist

- [ ] `jev_engine.py`: Add `if p.is_symlink(): continue` to the `rglob` loop in `select_target_files()`
- [ ] `jev_logging.py`: Expand `_SECRET_MARKERS` with `"ts_"`, `"bearer "`, `"authorization:"`
- [ ] `jev_logging.py`: Add length-based heuristic to `_redact()`
- [ ] `config.py`: Add `ensure_dotenv()` function
- [ ] `jev_mcp.py`: Replace inline dotenv block with `from config import ensure_dotenv; ensure_dotenv()`
- [ ] `jev_engine.py`: Remove inline dotenv block (lines 8–17), add `ensure_dotenv()` call in `__main__` only
- [ ] Run `pytest tests/ -v` — all tests green
