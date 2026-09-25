# Phase 3: Add Input Guardrails (Security)

> **Priority:** 🟡 High
> **Estimated effort:** ~30 minutes
> **Files to modify:** `jev_mcp.py`, `jev_engine.py`, `jev_errors.py`

---

## Task 3A: Add Input Length Validation on Tool Parameters

### Problem

The four MCP tool functions accept `command`, `task`, and `root_dir` string parameters with **no length limits**. A malicious or buggy caller can send a 10MB command string which goes through `fit_state()` → `stringify_state()` → `estimate_tokens()` → binary-search truncation — all expensive CPU work — before any useful work happens.

### Where to look

- File: `jev_mcp.py`, lines 57–78 (the four `@mcp.tool()` functions)
- File: `jev_engine.py`, lines 309 (`verify_command`), 374 (`find_agent_resources`), 523 (`select_target_files`), 595 (`select_model_tier`)

### Exact fix

Add input validation at the MCP tool level (the entry point) so expensive processing is never started. Add a constant and a validation helper.

**In `jev_mcp.py`**, add at the top (after imports):

```python
from jev_errors import JevValidationError

#: Maximum allowed length for any single tool parameter string.
MAX_INPUT_CHARS = 100_000  # 100KB — generous but prevents abuse


def _validate_input_length(**params):
    """Reject inputs over MAX_INPUT_CHARS before any processing."""
    for name, value in params.items():
        if isinstance(value, str) and len(value) > MAX_INPUT_CHARS:
            raise JevValidationError(
                f"Parameter '{name}' exceeds the maximum allowed length "
                f"({len(value):,} chars > {MAX_INPUT_CHARS:,} limit)."
            )
```

**Update each tool function** to validate inputs before calling the engine:

```python
@mcp.tool()
def guardrail_command(command: str) -> dict:
    """Check whether a terminal shell command is safe to execute or potentially destructive."""
    _validate_input_length(command=command)
    return _run("guardrail_command", lambda: verify_command(command), command=command)


@mcp.tool()
def search_agent_skills(task: str, root_dir: str = ".") -> dict:
    """Find and retrieve relevant agent skills, workflows, and memory markdown files for a given task."""
    _validate_input_length(task=task, root_dir=root_dir)
    return _run("search_agent_skills", lambda: find_agent_resources(task=task, root_dir=root_dir), task=task, root_dir=root_dir)


@mcp.tool()
def search_target_files(task: str, root_dir: str = ".") -> dict:
    """Identify which workspace files are relevant to a task using Jev AI evaluation."""
    _validate_input_length(task=task, root_dir=root_dir)
    return _run("search_target_files", lambda: select_target_files(task=task, root_dir=root_dir), task=task, root_dir=root_dir)


@mcp.tool()
def select_model_tier(task: str) -> dict:
    """Select the optimal LLM model tier (fast, balanced, or frontier) based on task complexity."""
    _validate_input_length(task=task)
    return _run("select_model_tier", lambda: _engine_select_model_tier(task), task=task)
```

The `_validate_input_length()` call goes **before** `_run()` so the validation error is caught by `_run()`'s exception handler and returned as a proper error envelope.

Actually, looking at this more carefully, `_validate_input_length` should be **inside** the `_run()` lambda or called within `_run()` so its exception gets mapped through `error_details()`. Let me reconsider.

Better approach — put the validation **inside** the lambda so `_run()` catches it:

```python
@mcp.tool()
def guardrail_command(command: str) -> dict:
    """Check whether a terminal shell command is safe to execute or potentially destructive."""
    def _do():
        _validate_input_length(command=command)
        return verify_command(command)
    return _run("guardrail_command", _do, command=command)
```

Or even simpler — add validation at the engine function level, since that's where the actual processing starts. This is cleaner because the engine functions are also callable from the CLI.

**Preferred approach — validate in each engine function:**

In `jev_engine.py`, add a helper near the top:

```python
MAX_INPUT_CHARS = 100_000


def _check_input_length(name: str, value: str) -> None:
    """Reject oversized string inputs before expensive processing."""
    if len(value) > MAX_INPUT_CHARS:
        raise JevValidationError(
            f"Parameter '{name}' is too long ({len(value):,} chars, limit {MAX_INPUT_CHARS:,})."
        )
```

Then add at the start of each engine function:

```python
def verify_command(command: str) -> dict:
    _check_input_length("command", command)
    # ... rest of function

def find_agent_resources(task: str, root_dir: str = ".", max_matches: int = 5) -> dict:
    _check_input_length("task", task)
    # ... rest of function

def select_target_files(task: str, root_dir: str = ".", max_results: int = 5) -> dict:
    _check_input_length("task", task)
    # ... rest of function

def select_model_tier(task: str) -> dict:
    _check_input_length("task", task)
    # ... rest of function
```

Note: `JevValidationError` is already imported in `jev_engine.py` via `from jev_errors import ...` — check if it's in the import. If not, add it. Looking at line 28: `from jev_errors import JevConfigError, JevResponseError, JevTimeoutError, error_details` — `JevValidationError` is NOT imported. Add it:

```python
from jev_errors import JevConfigError, JevResponseError, JevTimeoutError, JevValidationError, error_details
```

`error_details()` already handles `JevValidationError` (line 63) → returns `{"code": "INVALID_INPUT", "retryable": false}`.

---

## Task 3B: Validate `root_dir` Against Directory Traversal

### Problem

`root_dir` is accepted as-is and resolved with `Path(root_dir).resolve()`. A caller can pass `/etc`, `C:\Windows`, or `../../../` and the MCP will:
1. Walk the entire directory tree via `rglob("*")`
2. Read markdown file contents and return them in the response

### Where to look

- File: `jev_engine.py`, lines 375 (`find_agent_resources`) and 524 (`select_target_files`)

### Exact fix

Add a `_validate_root_dir()` function:

```python
def _validate_root_dir(root_dir: str) -> Path:
    """Resolve and sanity-check root_dir. Rejects system-level paths."""
    root = Path(root_dir).resolve()
    # Block obvious system roots
    blocked = {Path("/").resolve(), Path("/etc").resolve(), Path("/usr").resolve()}
    if os.name == "nt":
        for drive in "CDEFGH":
            blocked.add(Path(f"{drive}:\\Windows").resolve())
            blocked.add(Path(f"{drive}:\\").resolve())
    if root in blocked:
        raise JevValidationError(f"root_dir '{root_dir}' points to a system directory.")
    if not root.is_dir():
        raise JevValidationError(f"root_dir '{root_dir}' does not exist or is not a directory.")
    return root
```

Replace `root = Path(root_dir).resolve()` in both `find_agent_resources()` (line 375) and `select_target_files()` (line 524) with:

```python
root = _validate_root_dir(root_dir)
```

### Design decisions

- This is a **best-effort** guard, not a security sandbox. The MCP server already runs in the user's context with their filesystem permissions.
- We block only the most obviously dangerous paths (filesystem roots, Windows directory).
- We require `root_dir` to be an existing directory — prevents scanning of arbitrary file paths.

---

## Checklist

- [ ] `jev_engine.py`: Add `JevValidationError` to the import from `jev_errors`
- [ ] `jev_engine.py`: Add `MAX_INPUT_CHARS` constant and `_check_input_length()` helper
- [ ] `jev_engine.py`: Add `_check_input_length()` call to `verify_command()`, `find_agent_resources()`, `select_target_files()`, `select_model_tier()`
- [ ] `jev_engine.py`: Add `_validate_root_dir()` helper
- [ ] `jev_engine.py`: Replace `Path(root_dir).resolve()` with `_validate_root_dir(root_dir)` in `find_agent_resources()` and `select_target_files()`
- [ ] Run `pytest tests/ -v` — all tests green (tests use `tmp_path` which is a valid dir)
