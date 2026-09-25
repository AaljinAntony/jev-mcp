# Parallel Worker 3: Limits, Aliasing, Mock & Validation Hardening

> **Target Agent**: Agent Chat 3 (runs concurrently with Agents 1, 2, 4)  
> **Exclusive File Ownership**:
> - [`limits.py`](file:///d:/mcp/jev-typesafe-mcp/limits.py)
> - [`mock.py`](file:///d:/mcp/jev-typesafe-mcp/mock.py)
> - [`jev_validation.py`](file:///d:/mcp/jev-typesafe-mcp/jev_validation.py)
> - [`tests/test_limits.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_limits.py)
> - [`tests/test_validation.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_validation.py)  
> ⚠️ **CRITICAL LOCK RULE**: Do NOT touch `jev_engine.py`, `config.py`, or any other file.

---

## 1. Tasks Overview
1. Clean up `limits.py`: import `math` and `copy` at the top; remove inline `__import__("math")`.
2. Optimize `fit_state()`: avoid re-computing token count when input was not truncated (`state_tokens = estimate_tokens(fitted) if truncated else tokens`).
3. Defensively copy mutable containers (`dict`, `list`) in `fit_state()` when `truncated is False` to prevent state mutation aliasing.
4. Ensure Score probability keys are normalized to `int` in `mock.py` and `jev_validation.py`.

---

## 2. Exact Changes

### 2.1 `limits.py`
Add top-level imports:
```python
import copy
import json
import math

from jev_errors import JevBudgetError
```

Update `estimate_tokens`:
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

Update `fit_state` (lines 104–123):
```python
    truncated = tokens > budget
    fitted = truncate_to_token_budget(raw, budget) if truncated else raw
    evaluated = fitted
    if truncated and len(fitted) >= len(TRUNCATION_MARKER):
        evaluated = fitted[: -len(TRUNCATION_MARKER)]

    # Performance optimization: reuse pre-calculated tokens if state wasn't truncated
    state_tokens = estimate_tokens(fitted) if truncated else tokens

    # Defensive copy: prevent caller from mutating internal returned state
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

### 2.2 `mock.py` & `jev_validation.py`
Ensure `mock.py` and `jev_validation.py` normalize distribution keys to `int`.
In `mock.py` lines 114–122:
```python
    return ScoreAnswer(
        score=score,
        probabilities={int(k): p for k, p in probabilities.items()},
        legend={int(k): v for k, v in legend.items()},
        confidence=confidence_from_probabilities(probabilities),
    )
```

In `jev_validation.py` lines 102–109:
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

## 3. Verification Command
```powershell
.venv\Scripts\pytest tests/test_limits.py tests/test_validation.py -v
```
All tests must pass cleanly.
