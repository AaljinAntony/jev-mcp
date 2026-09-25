# Phase 1: Fix Error Ordering, Non-Skippable Tests, and Client Retries

> **Phase**: 01  
> **Target Files**:
> - [`jev_errors.py`](file:///d:/mcp/jev-typesafe-mcp/jev_errors.py)
> - [`jev_engine.py`](file:///d:/mcp/jev-typesafe-mcp/jev_engine.py)
> - [`tests/test_errors.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_errors.py)  
> **Parallel Execution Track**:
> - In parallel mode:
>   - **Track A Agent**: Modifies `jev_errors.py` and `tests/test_errors.py`.
>   - **Track E Agent**: Modifies `jev_engine.py`.
> - In sequential mode: Single agent applies all changes.

---

## 1. Problem Context & Rationale

### Issue 1: Fragile Exception Tree & Skipped Test
In `jev_errors.py`, `error_details()` maps exceptions into `{code, message, retryable}`.
The TypeSafe SDK exception hierarchy is:
```text
TypeSafeError (base)
  ├── TypeSafeAPIError (carries .status HTTP code)
  │     └── TypeSafeAPIResponseValidationError
  └── TypeSafeAPIConnectionError (inherits TypeSafeError + ConnectionError)
        └── TypeSafeAPITimeoutError (inherits TypeSafeAPIConnectionError + TimeoutError)
```

Notice:
1. `TypeSafeAPIConnectionError` does **not** inherit from `TypeSafeAPIError`.
2. It **does** inherit from `TypeSafeError`.
3. If an engineer reorders the handlers or puts `TypeSafeError` before `TypeSafeAPIConnectionError`, connection errors are misclassified as generic non-retryable failures.
4. In `tests/test_errors.py`, the test `test_sdk_connection_error` currently uses `pytest.skip` when constructor instantiation fails:
   ```python
   # tests/test_errors.py:88
   try:
       err = TypeSafeAPIConnectionError(request=None)
   except Exception:
       pytest.skip(...)
   ```
   Because this test skips, any regression in connection error handling goes unnoticed in CI!

### Issue 2: Missing Retry Configuration on Direct Invocations
When calling the TypeSafe API, network hiccups or transient HTTP 429 / 503 errors should automatically be retried using exponential backoff with jitter.
`TypeSafeClient` supports `RetryPolicy`. The MCP server should configure a standard retry policy on the cached client instance.

---

## 2. Step-by-Step Code Changes

### Step 2.1: `jev_errors.py`
Add explicit architectural documentation of the exception hierarchy above `error_details()`, verifying the order.

**Target lines** (`jev_errors.py` lines 54–62):
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

Ensure the branch sequence remains:
1. `JevBudgetError`
2. `JevValidationError`
3. `JevConfigError`
4. `JevResponseError`
5. `JevTimeoutError`
6. `JevCancelledError`
7. `TypeSafeAPITimeoutError`
8. `TypeSafeAPIResponseValidationError`
9. `TypeSafeAPIError` (checks HTTP status via `_retryable_status`)
10. `TypeSafeAPIConnectionError` (returns `retryable: True`, message "Could not connect to TypeSafe.")
11. `TypeSafeError` (catch-all for SDK internal errors, `retryable: False`)
12. `Exception` (catch-all `INTERNAL_ERROR`)

---

### Step 2.2: `jev_engine.py`
Ensure `RetryPolicy` is imported and used when initializing `TypeSafeClient`.

**Target lines** (`jev_engine.py` line 9):
```python
from typesafe_sdk import TypeSafeClient, Choice, Noul, Score, RetryPolicy
```

**Target lines** (`jev_engine.py` line 219–229 in `get_client()`):
```python
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
```

---

### Step 2.3: `tests/test_errors.py`
Replace the skippable `TypeSafeAPIConnectionError` test with a robust, non-skippable test.

**Target lines** (`tests/test_errors.py` around line 88):
Inspect how `TypeSafeAPIConnectionError` is initialized in `typesafe_sdk`. If `TypeSafeAPIConnectionError` requires a request or message, check `request=httpx.Request(...)` or inspect `TypeSafeAPIConnectionError.__init__`. Alternatively, instantiate or subclass:
```python
def test_sdk_connection_error():
    """Verify that pure TypeSafeAPIConnectionError maps to retryable API_ERROR."""
    try:
        # Standard SDK constructor
        err = TypeSafeAPIConnectionError(request=None)
    except TypeError:
        try:
            import httpx
            req = httpx.Request("POST", "https://api.typesafe.ai")
            err = TypeSafeAPIConnectionError(request=req)
        except Exception:
            # Fallback subclass to guarantee inheritance test never skips
            class MockConnectionError(TypeSafeAPIConnectionError):
                def __init__(self):
                    super(Exception, self).__init__("Connection dropped")

            err = MockConnectionError()

    envelope = error_details(err)
    assert envelope["code"] == "API_ERROR"
    assert envelope["retryable"] is True
    assert "Could not connect to TypeSafe" in envelope["message"]
```

Also add a test verifying that `TypeSafeAPITimeoutError` resolves to `TIMEOUT` code and does not get swallowed by the generic connection error:
```python
def test_sdk_timeout_error_precedence():
    """Verify TypeSafeAPITimeoutError is caught before TypeSafeAPIConnectionError."""
    try:
        err = TypeSafeAPITimeoutError(request=None)
    except Exception:
        class MockTimeoutError(TypeSafeAPITimeoutError):
            def __init__(self):
                super(Exception, self).__init__("Request timed out")
        err = MockTimeoutError()

    envelope = error_details(err)
    assert envelope["code"] == "TIMEOUT"
    assert envelope["retryable"] is True
```

---

## 3. Verification Commands

Run the error test suite:
```powershell
.venv\Scripts\pytest tests/test_errors.py -v
```

Expected output:
- `tests/test_errors.py` passes with **0 skipped tests**.
- All envelope mappings verify status code, retryable flag, and message sanitization.

---

## 4. Acceptance Criteria
- [ ] `jev_errors.py` has documented inheritance hierarchy comments.
- [ ] `TypeSafeAPIConnectionError` always returns `code="API_ERROR"`, `retryable=True`.
- [ ] `tests/test_errors.py` contains zero skipped tests for connection error handling.
- [ ] `jev_engine.py` constructs `TypeSafeClient` with `RetryPolicy(max_retries=2, ...)`.
- [ ] `pytest tests/test_errors.py` passes completely.
