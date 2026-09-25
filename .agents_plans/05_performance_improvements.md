# Phase 5: Performance Improvements — Logging, Token Estimation & Git Discovery

> **Phase**: 05  
> **Target Files**:
> - [`jev_logging.py`](file:///d:/mcp/jev-typesafe-mcp/jev_logging.py)
> - [`limits.py`](file:///d:/mcp/jev-typesafe-mcp/limits.py)
> - [`jev_engine.py`](file:///d:/mcp/jev-typesafe-mcp/jev_engine.py)  
> **Parallel Execution Track**:
> - In parallel mode:
>   - **Track B Agent**: Edits `jev_logging.py`.
>   - **Track C Agent**: Edits `limits.py`.
>   - **Track E Agent**: Edits `jev_engine.py`.
> - In sequential mode: Single agent applies all changes.

---

## 1. Problem Context & Rationale

### Optimization 1: Duplicate `json.dumps(result)` in Logging
In `jev_logging.py` lines 107–109:
```python
payload = {"result_len": len(json.dumps(result, default=str))}
preview = json.dumps(result, default=str)[:1000]
payload["result_preview"] = preview
```
`json.dumps(result, default=str)` is executed twice in full! When tools return large payloads (e.g. hundreds of candidate files, long skill contents, or full JSON responses), this doubles serialization time and creates unnecessary garbage collection pressure on every single tool call.

### Optimization 2: Redundant `estimate_tokens` Call in `fit_state()`
In `limits.py` lines 103–123:
```python
raw = stringify_state(state)
tokens = estimate_tokens(raw)
truncated = tokens > budget
fitted = truncate_to_token_budget(raw, budget) if truncated else raw
...
"estimated_tokens": {
    "state": estimate_tokens(fitted), # <-- Calls estimate_tokens again!
    "questions": questions_tokens,
    "longest_question": longest_tokens,
}
```
When `truncated` is `False`, `fitted` is identical to `raw`. Calling `estimate_tokens(fitted)` re-iterates through the entire string character-by-character to recount ASCII and non-ASCII chars when the exact value is already stored in `tokens`.

### Optimization 3: Redundant `.git` Parent Walk in `_discover_files_git()`
In `jev_engine.py` line 609:
```python
if not (root / ".git").exists() and not any((p / ".git").exists() for p in root.parents):
    return None
```
Walking up all parent directory paths probing for `.git` is slow on deeply nested directory trees or network mounts. `git ls-files` internally traverses parent directories to discover the git working tree in optimized C and fails immediately with `returncode != 0` if not in a repository. The Python pre-check is completely redundant.

---

## 2. Step-by-Step Code Changes

### Step 2.1: `jev_logging.py` — Single-Pass JSON Serialization

**Target lines** (`jev_logging.py` lines 102–113):
```python
def log_tool_call(tool: str, ms: int, args=None, result=None, error=None) -> None:
    """Log an MCP tool invocation (args/value serialized, secrets redacted)."""
    if error is not None:
        payload = {"error": error}
    else:
        # Serialize once, derive both length and preview
        dumped = json.dumps(result, default=str)
        payload = {
            "result_len": len(dumped),
            "result_preview": dumped[:1000],
        }
    args_safe = {k: _redact(v) for k, v in (args or {}).items()}
    log_event("tool_call", tool=tool, args=args_safe, ms=int(ms), **payload)
```

---

### Step 2.2: `limits.py` — Reuse Token Estimation

**Target lines** (`limits.py` lines 102–123):
```python
    raw = stringify_state(state)
    tokens = estimate_tokens(raw)
    truncated = tokens > budget
    fitted = truncate_to_token_budget(raw, budget) if truncated else raw
    evaluated = fitted
    if truncated and len(fitted) >= len(TRUNCATION_MARKER):
        evaluated = fitted[: -len(TRUNCATION_MARKER)]

    # Optimization: reuse tokens count if state was not modified/truncated
    state_tokens = estimate_tokens(fitted) if truncated else tokens

    return {
        "state": fitted if truncated else state,
        "truncated": truncated,
        "coverage": {
            "complete": not truncated,
            "original_chars": len(raw),
            "evaluated_chars": len(evaluated),
            "estimated_tokens": {
                "state": state_tokens,
                "questions": questions_tokens,
                "longest_question": longest_tokens,
            },
            "estimator": "chars/4",
        },
    }
```

---

### Step 2.3: `jev_engine.py` — Streamline `_discover_files_git()`

**Target lines** (`jev_engine.py` lines 606–621):
Remove the redundant parent `.git` walk and rely directly on `git ls-files`:

```python
def _discover_files_git(
    root: Path,
    ignore_exts: set,
    max_count: int,
    ignore_dirs: Optional[set] = None,
) -> Optional[List[str]]:
    """Use git ls-files for fast, .gitignore-aware file discovery. Returns None if not a git repo."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        candidates = []
        dirs_to_ignore = ignore_dirs or set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            p = Path(line)
            if any(ignored in p.parts for ignored in dirs_to_ignore):
                continue
            if p.suffix.lower() in ignore_exts:
                continue
            if (root / p).is_file():
                candidates.append(line.replace("\\", "/"))
            if len(candidates) >= max_count:
                break
        return candidates
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
```

---

## 3. Verification Commands

Run unit tests for limits, logging, and engine file discovery:
```powershell
.venv\Scripts\pytest tests/test_limits.py tests/test_mock_tools.py -v
```

Expected output:
- `test_limits.py` passes with identical coverage shapes and token counts.
- `test_mock_tools.py` passes all tool runs.

---

## 4. Acceptance Criteria
- [ ] `jev_logging.py` calls `json.dumps(result)` exactly once per successful tool call.
- [ ] `limits.fit_state()` reuses precomputed `tokens` when `truncated is False`.
- [ ] `_discover_files_git()` skips parent `.git` checking and lets `git ls-files` fail fast.
- [ ] All tests pass cleanly without behavioral changes.
