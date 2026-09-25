# Phase 2: Fix Remaining Correctness Bugs

> **Priority:** 🟡 High
> **Estimated effort:** ~1.5 hours
> **Files to modify:** `jev_engine.py`, `jev_validation.py`, `mock.py`

---

## Task 2A: Refactor `verify_command()` to Use `_request()` Helper

### Problem

`verify_command()` in `jev_engine.py` (lines 309–361) manually calls `get_client()`, `fit_state()`, `execute_system_one()`, and `validate_response()` inline. The other three tools (`find_agent_resources`, `select_target_files`, `select_model_tier`) use the shared `_request()` helper (lines 282–303) which also:

1. Calls `log_round()` to record provider round-trip timing
2. Provides a single choke-point for future cross-cutting concerns (circuit breaker, caching, etc.)

Because `verify_command()` bypasses `_request()`, guardrail tool calls produce **no `round_ok` / `round_error` log entries** — making them invisible in production diagnostics.

### Where to look

- File: `jev_engine.py`, lines 282–303 (`_request()` helper)
- File: `jev_engine.py`, lines 309–361 (`verify_command()`)

### Current code (lines 309–324)

```python
def verify_command(command: str) -> dict:
    client = get_client()
    state = f"Terminal shell command to execute: {command}"
    questions = {
        "is_destructive": Noul(
            instructions="Does this command permanently delete files, drop tables, or wipe directories?"
        ),
        "modifies_git": Noul(
            instructions="Does this command modify or delete git configuration or history (e.g. force push, rm -rf .git)?"
        )
    }

    cfg = get_config()
    fitted = fit_state(state, questions)
    res = execute_system_one(client, state=fitted["state"], questions=questions)
    validate_response(res, questions)
    # ... rest of function uses res, fitted, cfg
```

### Exact fix

Replace lines 309–324 with:

```python
def verify_command(command: str) -> dict:
    state = f"Terminal shell command to execute: {command}"
    questions = {
        "is_destructive": Noul(
            instructions="Does this command permanently delete files, drop tables, or wipe directories?"
        ),
        "modifies_git": Noul(
            instructions="Does this command modify or delete git configuration or history (e.g. force push, rm -rf .git)?"
        )
    }

    res, fitted, cfg = _request(state, questions)
    # ... rest of function remains EXACTLY the same from line 326 onward
```

### What changes

- Remove the explicit calls to `get_client()`, `get_config()`, `fit_state()`, `execute_system_one()`, `validate_response()`
- Replace them with `res, fitted, cfg = _request(state, questions)`
- The `_request()` helper already returns `(res, fitted, cfg)` — exactly what `verify_command()` needs
- All lines after `res, fitted, cfg = _request(...)` remain unchanged (the probability/confidence/action/safe logic starting around line 326)

### What NOT to change

- Do NOT modify `_request()` itself
- Do NOT change the guardrail policy logic (lines 326–361)
- Do NOT change the return shape — it must remain backward-compatible

### Validation

Run:
```bash
pytest tests/test_mock_tools.py::TestGuardrailTool -v
```

All 5 guardrail tests must pass. Additionally, verify that the log file (`logs/jev_engine.log`) now contains `round_ok` entries when running:
```bash
python jev_engine.py verify "git status"
```

---

## Task 2B: Fix Score Validation Key Type Mismatch

### Problem

In `jev_validation.py`, the `_validate_score()` function (lines 88–124) has a key type mismatch between the probabilities dict and the expected keys list.

On line 108–109:
```python
numeric_probs = {k: float(probabilities[k]) for k in probabilities}
_validate_probabilities(numeric_probs, expected_int, name, "score")
```

- `numeric_probs` has the **original keys** from the probabilities dict. When the response comes as raw JSON (deserialized dict), these are **strings** (e.g. `"0"`, `"1"`, `"2"`).
- `expected_int` is `list(range(n))` — a list of **integers** (e.g. `[0, 1, 2]`).
- `_validate_probabilities()` (line 49) does `set(actual) != set(keys_expected)` — `{"0","1","2"} != {0,1,2}` → **validation always fails for raw dict answers with string keys**.

This currently works in the codebase only because:
1. The mock (`mock.py`) explicitly constructs `ScoreAnswer` with `int()` keys
2. The SDK's `ScoreAnswer` object likely uses integer keys internally

But a response that arrives as a raw JSON dict (e.g. from a proxy or a future SDK version) would spuriously fail validation.

### Where to look

- File: `jev_validation.py`, lines 101–109

### Current code

```python
try:
    normalized_keys = [_int_key(k) for k in probabilities.keys()]
except (ValueError, TypeError):
    raise JevResponseError(f"answer '{name}' probabilities must be keyed by score level")
expected_int = list(range(n))
if sorted(normalized_keys) != expected_int:
    raise JevResponseError(f"answer '{name}' probabilities must cover exactly the score levels 0..{n - 1}")
numeric_probs = {k: float(probabilities[k]) for k in probabilities}
_validate_probabilities(numeric_probs, expected_int, name, "score")
```

### Exact fix

Build `numeric_probs` with **integer keys** (the already-normalized keys) so it matches `expected_int`:

```python
try:
    normalized_keys = [_int_key(k) for k in probabilities.keys()]
except (ValueError, TypeError):
    raise JevResponseError(f"answer '{name}' probabilities must be keyed by score level")
expected_int = list(range(n))
if sorted(normalized_keys) != expected_int:
    raise JevResponseError(f"answer '{name}' probabilities must cover exactly the score levels 0..{n - 1}")
# Build numeric_probs with normalized int keys to match expected_int
numeric_probs = {_int_key(k): float(probabilities[k]) for k in probabilities}
_validate_probabilities(numeric_probs, expected_int, name, "score")
```

The only change is on the `numeric_probs = ...` line: use `_int_key(k)` instead of `k` as the dict key.

### Test to add

Add to `tests/test_validation.py` to verify string-keyed score responses validate correctly:

```python
def test_score_with_string_keys_accepted(self):
    """Score probabilities with string keys (raw JSON) should pass validation."""
    questions = {"q": Score(criteria=["low", "mid", "high"], instructions="Rate")}
    answers = {
        "q": {
            "type": "score",
            "score": 1.0,
            "confidence": 0.8,
            "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1},  # string keys
            "legend": {"0": "low", "1": "mid", "2": "high"},     # string keys
        }
    }
    assert validate_response(_response(answers), questions) is not None
```

Place this in the `TestStructure` class alongside the existing `test_valid_score_response`.

### Validation

Run:
```bash
pytest tests/test_validation.py -v
```

All existing tests must pass plus the new string-keys test.

---

## Task 2C: Fix `relative_to()` Crash + Remove Dead Code

### Problem 1: `find_agent_resources` — `p.relative_to(root)` ValueError

In `jev_engine.py` line 384:
```python
rel = p.relative_to(root).as_posix()
```

`get_scan_paths()` resolves paths via `(root / rel).resolve()`. If a configured `scan_path` contains `..` or follows a symlink, the resolved path can end up **outside** `root`. `Path.relative_to()` raises `ValueError` when the path is not a descendant of the base.

### Where to look

- File: `jev_engine.py`, lines 378–385

### Exact fix

Use a try/except to skip files that can't be made relative:

```python
candidate_files: Dict[str, Path] = {}
for sdir in search_dirs:
    if not sdir.exists():
        continue
    for p in sdir.rglob("*.md"):
        if p.is_file():
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue  # path is outside root, skip it
            candidate_files[rel] = p
```

### Problem 2: Dead code in `mock.py`

In `mock.py` lines 80–81:
```python
if label == "is_destructive" or label == "modifies_git":
    pass
```

This is a no-op branch inside `_score_option()`. The labels `is_destructive` and `modifies_git` are Noul question IDs that would never appear in a Choice scoring context. Remove these two lines entirely.

### Validation

Run:
```bash
pytest tests/ -v
```

All tests must pass.

---

## Checklist

- [x] `jev_engine.py` `verify_command()`: Replace inline calls with `res, fitted, cfg = _request(state, questions)`
- [x] `jev_validation.py` line 108: Change `numeric_probs = {k: ...}` to `numeric_probs = {_int_key(k): ...}`
- [x] `tests/test_validation.py`: Add `test_score_with_string_keys_accepted` test
- [x] `jev_engine.py` line 384: Wrap `relative_to()` in try/except ValueError
- [x] `mock.py` lines 80–81: Delete the dead `is_destructive`/`modifies_git` branch
- [x] Run `pytest tests/ -v` — all tests green
