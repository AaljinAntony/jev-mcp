# Phase 1: Fix Error Ordering + Add SDK Retries (Critical)

> **Priority:** 🔴 Critical — These are the two highest-impact issues in the codebase.
> **Estimated effort:** ~1 hour
> **Files to modify:** `jev_errors.py`, `jev_engine.py`, `tests/test_validation.py` (or new test file)

---

## Task 1A: Fix `error_details()` — Unreachable `TypeSafeAPIResponseValidationError` Branch

### Problem

In `jev_errors.py`, the `error_details()` function (line 54–96) checks exception types using `isinstance()` in the wrong order. The exception class hierarchy is:

```
TypeSafeError
  └─ TypeSafeAPIError              ← checked on line 84
       └─ TypeSafeAPIResponseValidationError  ← checked on line 90 (UNREACHABLE)
  └─ TypeSafeAPIConnectionError    ← checked on line 92
       └─ TypeSafeAPITimeoutError  ← checked on line 77 (correct, before connection)
```

Because `TypeSafeAPIResponseValidationError` is a **subclass** of `TypeSafeAPIError`, the `isinstance(err, TypeSafeAPIError)` check on **line 84** catches it first. The branch on **line 90** is dead code. This means a response validation error gets classified as a generic `API_ERROR` with `retryable` based on its HTTP status code, instead of `INVALID_RESPONSE` with `retryable: False`.

### Where to look

- File: `jev_errors.py`, lines 84–95
- The current order is:
  ```python
  # Line 84
  if isinstance(err, TypeSafeAPIError):
      return {"code": "API_ERROR", ...}
  # Line 90 — DEAD CODE
  if isinstance(err, TypeSafeAPIResponseValidationError):
      return {"code": "INVALID_RESPONSE", ...}
  # Line 92
  if isinstance(err, TypeSafeAPIConnectionError):
      ...
  ```

### Exact fix

Move the `TypeSafeAPIResponseValidationError` check **before** the `TypeSafeAPIError` check. The corrected order should be:

```python
# 1. Check TypeSafeAPIResponseValidationError FIRST (subclass of TypeSafeAPIError)
if isinstance(err, TypeSafeAPIResponseValidationError):
    return {"code": "INVALID_RESPONSE", "message": str(err), "retryable": False}
# 2. Then check TypeSafeAPIError (the parent)
if isinstance(err, TypeSafeAPIError):
    return {
        "code": "API_ERROR",
        "message": f"TypeSafe API request failed (HTTP {err.status}).",
        "retryable": _retryable_status(err.status),
    }
# 3. TypeSafeAPIConnectionError stays after (it's a sibling, not a subclass of TypeSafeAPIError)
if isinstance(err, TypeSafeAPIConnectionError):
    return {"code": "API_ERROR", "message": "Could not connect to TypeSafe.", "retryable": True}
```

### Test to add

Add a test (in a new file `tests/test_errors.py` or in an existing test file) that verifies the fix:

```python
import pytest
from typesafe_sdk import TypeSafeAPIResponseValidationError
from jev_errors import error_details

class TestErrorDetails:
    def test_response_validation_error_is_invalid_response(self):
        """TypeSafeAPIResponseValidationError must NOT be caught by the TypeSafeAPIError branch."""
        err = TypeSafeAPIResponseValidationError(
            status=200,
            body={"error": "missing field"},
            headers={},
            field_path="answers.tone.confidence",
            endpoint="POST /v1/system_one",
        )
        result = error_details(err)
        assert result["code"] == "INVALID_RESPONSE"
        assert result["retryable"] is False

    def test_generic_api_error_is_api_error(self):
        """A plain TypeSafeAPIError should still be API_ERROR."""
        from typesafe_sdk import TypeSafeAPIError
        err = TypeSafeAPIError(status=500, body=None, headers={}, endpoint="POST /v1/system_one")
        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True  # 500 is retryable

    def test_timeout_is_retryable(self):
        from typesafe_sdk import TypeSafeAPITimeoutError
        err = TypeSafeAPITimeoutError(timeout=30.0)
        result = error_details(err)
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True

    def test_connection_error_is_retryable(self):
        from typesafe_sdk import TypeSafeAPIConnectionError
        err = TypeSafeAPIConnectionError()
        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True
```

> **Note:** Check the exact constructor signatures of the SDK exception classes. The `TypeSafeAPIResponseValidationError` constructor expects `(status, body, headers, field_path, endpoint)` based on the SDK docs. Adjust if needed.

### Validation

Run `pytest tests/test_errors.py -v` to confirm the new tests pass.
Run `pytest tests/ -v` to confirm no existing tests break.

---

## Task 1B: Add SDK `RetryPolicy` to `get_client()`

### Problem

In `jev_engine.py`, the `get_client()` function (line 143–154) creates a `TypeSafeClient` with only `api_key` and `timeout`. The TypeSafe Python SDK (v0.7.1, as per `requirements.txt`) supports a `RetryPolicy` dataclass with exponential backoff, jitter, and retry-after header support — **none of this is configured**.

This means a single transient HTTP error (429 rate limit, 502 bad gateway, 503 service unavailable, or network blip) immediately kills the tool call. The MCP layer catches the exception and returns an error envelope, but the user sees a failure when a simple retry would have succeeded.

### Where to look

- File: `jev_engine.py`, lines 143–154
- Current code:
  ```python
  def get_client() -> Optional[TypeSafeClient]:
      cfg = get_config()
      if cfg.mock:
          return None
      if not cfg.api_key:
          raise JevConfigError("TYPESAFE_API_KEY environment variable is not configured.")
      return TypeSafeClient(api_key=cfg.api_key, timeout=cfg.timeout_ms / 1000.0)
  ```

### Exact fix

1. Add `RetryPolicy` to the imports at the top of `jev_engine.py` (line 20):

   ```python
   from typesafe_sdk import TypeSafeClient, Choice, Noul, Score, RetryPolicy
   ```

2. Update the `get_client()` function to pass a `RetryPolicy`:

   ```python
   def get_client() -> Optional[TypeSafeClient]:
       """Return a configured TypeSafe client, or None in mock mode.

       Raises `JevConfigError` when `TYPESAFE_API_KEY` is missing (unless
       `JEV_MCP_MOCK=1`, which never needs a key).
       """
       cfg = get_config()
       if cfg.mock:
           return None
       if not cfg.api_key:
           raise JevConfigError("TYPESAFE_API_KEY environment variable is not configured.")
       return TypeSafeClient(
           api_key=cfg.api_key,
           timeout=cfg.timeout_ms / 1000.0,
           retry=RetryPolicy(
               max_retries=2,
               backoff_initial=0.5,
               backoff_max=5.0,
               backoff_jitter=0.25,
           ),
       )
   ```

### Design decisions

- **`max_retries=2`**: Two retries after the initial attempt (3 total attempts). This is the SDK default and keeps total latency bounded.
- **`backoff_initial=0.5`**: Start with 0.5s delay. The SDK doubles this each retry up to `backoff_max`.
- **`backoff_max=5.0`**: Never wait more than 5 seconds between retries. Given the default 30s timeout, this keeps the retry budget within reason.
- **`backoff_jitter=0.25`**: 25% random jitter to prevent thundering herd on rate limits.
- The SDK's default `http_statuses` (`{408, 429, 500..599}`) and `respect_retry_after=True` are left as defaults — they match what `RETRYABLE_STATUSES` in `jev_errors.py` already considers retryable.

### What NOT to change

- The `execute_system_one()` function's exception mapping (lines 167–178) should remain as-is. SDK retries happen *inside* the client before the exception bubbles up. If all retries are exhausted, the SDK raises the original exception and `execute_system_one()` maps it correctly.
- Do NOT add application-level retry logic on top of SDK retries — that would cause retry amplification.

### Validation

- Run `pytest tests/ -v` — all existing tests should pass (mock mode doesn't use the client).
- Optionally run `python scripts/diag_mcp.py --tool guardrail_command --command "git status"` with a live API key to verify the client still works.
- Check that `RetryPolicy` is importable: `python -c "from typesafe_sdk import RetryPolicy; print('ok')"`.

---

## Checklist

- [ ] `jev_errors.py`: Move `TypeSafeAPIResponseValidationError` check before `TypeSafeAPIError`
- [ ] Add `tests/test_errors.py` with 4 error-ordering tests
- [ ] `jev_engine.py` line 20: Add `RetryPolicy` to imports
- [ ] `jev_engine.py` `get_client()`: Pass `RetryPolicy(max_retries=2, ...)`
- [ ] Run `pytest tests/ -v` — all tests green
