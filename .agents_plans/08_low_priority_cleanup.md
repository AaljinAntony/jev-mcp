# Phase 8: Low Priority Cleanup — Imports, State Aliasing, Score Keys & Graceful Shutdown

> **Phase**: 08  
> **Target Files**:
> - [`limits.py`](file:///d:/mcp/jev-typesafe-mcp/limits.py)
> - [`mock.py`](file:///d:/mcp/jev-typesafe-mcp/mock.py)
> - [`jev_validation.py`](file:///d:/mcp/jev-typesafe-mcp/jev_validation.py)
> - [`jev_mcp.py`](file:///d:/mcp/jev-typesafe-mcp/jev_mcp.py)  
> **Parallel Execution Track**:
> - In parallel mode:
>   - **Track C Agent**: Modifies `limits.py`, `mock.py`, and `jev_validation.py`.
>   - **Track E Agent**: Modifies `jev_mcp.py`.
> - In sequential mode: Single agent applies all changes.

---

## 1. Problem Context & Rationale

### Cleanup 1: Top-Level `import math` in `limits.py`
In `limits.py` line 34:
```python
return __import__("math").ceil(ascii_chars / 4 + other_chars)
```
Calling `__import__("math")` inside a tight per-invocation loop is unnecessary overhead. `math` should be imported once at module load time.

### Cleanup 2: `fit_state` Mutable State Aliasing
In `limits.py` line 110:
```python
return {
    "state": fitted if truncated else state,
    ...
}
```
If `state` is a mutable `dict` or `list` and `truncated` is `False`, returning `state` directly means the caller and any downstream consumer share the exact same object reference. If a consumer mutates keys or values in `fitted["state"]`, the original caller's data is silently altered.
**Fix**: Defensively copy mutable container objects (`dict`, `list`) when not truncated.

### Cleanup 3: Score Probability Key Normalization
In `mock.py` and `jev_validation.py`:
Live JSON responses from HTTP APIs parse keys as strings (`"0"`, `"1"`), whereas local python objects or mocks may key distributions with integers (`0`, `1`). Code accessing `probabilities[0]` vs `probabilities["0"]` must never throw a `KeyError`.
**Fix**: Verify and guarantee that `jev_validation.py` normalizes all distribution keys to `int`.

### Cleanup 4: Graceful Shutdown in `jev_mcp.py`
`jev_mcp.py` currently logs `server_start` on boot, but when the MCP host terminates the stdio process via SIGINT or SIGTERM, pending log events in the `RotatingFileHandler` buffer may not be flushed to disk.
**Fix**: Register an `atexit` handler to emit `server_stop` and flush handlers.

---

## 2. Step-by-Step Code Changes

### Step 2.1: `limits.py` — Math Import & Defensive Copying

**Target lines** (`limits.py` lines 1–15):
Add top-level imports:
```python
import copy
import json
import math

from jev_errors import JevBudgetError
```

**Target lines** (`limits.py` lines 24–35 in `estimate_tokens`):
```python
def estimate_tokens(value) -> int:
    """Rough token estimate: ASCII chars / 4 + non-ASCII chars."""
    text = stringify_state(value)
    ascii_chars = 0
    other_chars = 0
    for char in text:
        if ord(char) <= 0x7F:
            ascii_chars += 1
        else:
            other_chars += 1
    return math.ceil(ascii_chars / 4 + other_chars)
```

**Target lines** (`limits.py` lines 105–123 in `fit_state`):
```python
    # Defensively copy mutable containers when not truncated to avoid aliasing
    if truncated:
        output_state = fitted
    elif isinstance(state, (dict, list)):
        output_state = copy.deepcopy(state)
    else:
        output_state = state

    return {
        "state": output_state,
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

### Step 2.2: `jev_validation.py` — Ensure Integer Keyed Distribution

**Target lines** (`jev_validation.py` lines 102–110):
Confirm `numeric_probs` consistently normalizes string/int keys:
```python
    try:
        normalized_keys = [_int_key(k) for k in probabilities.keys()]
    except (ValueError, TypeError):
        raise JevResponseError(f"answer '{name}' probabilities must be keyed by score level")
    expected_int = list(range(n))
    if sorted(normalized_keys) != expected_int:
        raise JevResponseError(f"answer '{name}' probabilities must cover exactly the score levels 0..{n - 1}")
    numeric_probs = {_int_key(k): float(probabilities[k]) for k in probabilities}
```

---

### Step 2.3: `jev_mcp.py` — Graceful Shutdown Hook

**Target lines** (`jev_mcp.py` lines 25–30):
Add `atexit` registration:
```python
import atexit
import logging

mcp = FastMCP("jev-engine")

log_event("server_start", pid=os.getpid(), log_file=str(log_path()))


def _on_shutdown():
    try:
        log_event("server_stop", pid=os.getpid())
        for handler in logging.getLogger("jev_engine").handlers:
            handler.flush()
    except Exception:
        pass


atexit.register(_on_shutdown)
```

---

## 3. Verification Commands

Run validation, limits, and server tests:
```powershell
.venv\Scripts\pytest tests/test_validation.py tests/test_limits.py -v
```

Expected output:
- All validation tests pass.
- State mutation isolation tests pass.
- Server start and shutdown hooks initialize without error.

---

## 4. Acceptance Criteria
- [ ] `math` is imported at top level in `limits.py`.
- [ ] `fit_state()` deep-copies mutable dict/list inputs to prevent aliasing.
- [ ] `_validate_score()` handles string and int level keys transparently.
- [ ] `jev_mcp.py` registers an `atexit` shutdown handler that flushes log buffers.
