# Parallel Worker 1: Errors & Policy Hardening

> **Target Agent**: Agent Chat 1 (runs concurrently with Agents 2, 3, 4)  
> **Exclusive File Ownership**:
> - [`jev_errors.py`](file:///d:/mcp/jev-typesafe-mcp/jev_errors.py)
> - [`policy.py`](file:///d:/mcp/jev-typesafe-mcp/policy.py)
> - [`tests/test_errors.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_errors.py)
> - [`tests/test_policy.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_policy.py)  
> ⚠️ **CRITICAL LOCK RULE**: Do NOT touch `jev_engine.py`, `config.py`, or any other file.

---

## 1. Tasks Overview
1. Document SDK exception inheritance hierarchy in `jev_errors.py` and verify `TypeSafeAPIConnectionError` precedence.
2. Remove skippable test in `tests/test_errors.py` so connection error mapping is 100% verified.
3. Export named constants `DEFAULT_RISK_THRESHOLD = 0.20` and `DEFAULT_ESCALATE_THRESHOLD = 0.50` in `policy.py`.
4. Update `guardrail_safe()` in `policy.py` to use `DEFAULT_RISK_THRESHOLD`.
5. Optimize `_is_finite()` in `policy.py` (move `import math` to top of file).

---

## 2. Exact Changes

### 2.1 `jev_errors.py`
Add explicit comments documenting the inheritance tree above `error_details` (line 54):
```python
def error_details(err: Exception) -> dict:
    """Map any exception to a `{code, message, retryable}` envelope.

    Ordering is critical based on the SDK inheritance tree:
      TypeSafeError (base)
        ├── TypeSafeAPIError (HTTP status) -> check after ResponseValidationError
        │     └── TypeSafeAPIResponseValidationError -> check BEFORE TypeSafeAPIError
        └── TypeSafeAPIConnectionError (network) -> check BEFORE TypeSafeError
              └── TypeSafeAPITimeoutError -> check BEFORE TypeSafeAPIConnectionError
    """
```

### 2.2 `tests/test_errors.py`
Replace `test_sdk_connection_error` with a non-skippable test:
```python
def test_sdk_connection_error():
    """Verify that pure TypeSafeAPIConnectionError maps to retryable API_ERROR."""
    class MockConnectionError(TypeSafeAPIConnectionError):
        def __init__(self):
            super(Exception, self).__init__("Connection dropped")

    err = MockConnectionError()
    envelope = error_details(err)
    assert envelope["code"] == "API_ERROR"
    assert envelope["retryable"] is True
    assert "Could not connect to TypeSafe" in envelope["message"]
```

### 2.3 `policy.py`
Move `math` import to top-level:
```python
import math
from typing import Iterable, List, Literal

from jev_errors import JevValidationError
```

Define constants at lines 14–20:
```python
DEFAULT_AUTO_ACCEPT = 0.8
DEFAULT_REVIEW_AT = 0.5
DEFAULT_RISK_THRESHOLD = 0.20
DEFAULT_ESCALATE_THRESHOLD = 0.50
```

Update `guardrail_safe` default parameters:
```python
def guardrail_safe(
    action: PolicyAction,
    destructive_prob: float,
    git_modify_prob: float,
    destructive_threshold: float = DEFAULT_RISK_THRESHOLD,
    git_threshold: float = DEFAULT_RISK_THRESHOLD,
) -> bool:
    """Backward-compatible ``safe``: auto AND low destructive/git probabilities."""
    return (
        action == "auto"
        and destructive_prob < destructive_threshold
        and git_modify_prob < git_threshold
    )
```

Update `_is_finite`:
```python
def _is_finite(value: float) -> bool:
    try:
        return isinstance(value, (int, float)) and math.isfinite(value)
    except (TypeError, ValueError):
        return False
```

### 2.4 `tests/test_policy.py`
Add tests for thresholds:
```python
from policy import DEFAULT_RISK_THRESHOLD, DEFAULT_ESCALATE_THRESHOLD, guardrail_safe

def test_risk_constants_and_boundaries():
    assert DEFAULT_RISK_THRESHOLD == 0.20
    assert DEFAULT_ESCALATE_THRESHOLD == 0.50
    assert guardrail_safe("auto", 0.199, 0.199) is True
    assert guardrail_safe("auto", 0.20, 0.0) is False
```

---

## 3. Verification Command
```powershell
.venv\Scripts\pytest tests/test_errors.py tests/test_policy.py -v
```
All tests must pass with 0 skips.
