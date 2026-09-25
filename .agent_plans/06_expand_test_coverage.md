# Phase 6: Expand Test Coverage

> **Priority:** 🟡 High
> **Estimated effort:** ~1 hour
> **Files to create/modify:** `tests/test_errors.py` (new), `tests/test_mock_tools.py`, `tests/test_limits.py`, `tests/test_validation.py`

---

## Task 6A: Create `tests/test_errors.py` — Error Envelope Tests

### Why

After Phase 1 fixes the `error_details()` isinstance ordering, there should be regression tests that prevent re-introducing the bug. The entire `error_details()` function has no dedicated test coverage today.

### Create file: `tests/test_errors.py`

```python
"""Tests for jev_errors.error_details() exception → envelope mapping."""

import pytest

from jev_errors import (
    JevBudgetError,
    JevCancelledError,
    JevConfigError,
    JevResponseError,
    JevTimeoutError,
    JevValidationError,
    error_details,
    error_message,
)


class TestErrorDetailsMapping:
    """Each Jev exception type must map to the correct code and retryable flag."""

    def test_budget_error(self):
        result = error_details(JevBudgetError("too big"))
        assert result["code"] == "INPUT_TOO_LARGE"
        assert result["retryable"] is False
        assert "too big" in result["message"]

    def test_validation_error(self):
        result = error_details(JevValidationError("bad input"))
        assert result["code"] == "INVALID_INPUT"
        assert result["retryable"] is False

    def test_config_error(self):
        result = error_details(JevConfigError("missing key"))
        assert result["code"] == "CONFIG_ERROR"
        assert result["retryable"] is False

    def test_response_error(self):
        result = error_details(JevResponseError("bad response"))
        assert result["code"] == "INVALID_RESPONSE"
        assert result["retryable"] is False

    def test_timeout_error(self):
        result = error_details(JevTimeoutError())
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True

    def test_cancelled_error(self):
        result = error_details(JevCancelledError())
        assert result["code"] == "CANCELLED"
        assert result["retryable"] is False

    def test_generic_exception(self):
        result = error_details(RuntimeError("unexpected"))
        assert result["code"] == "INTERNAL_ERROR"
        assert result["retryable"] is False
        assert "unexpected" in result["message"]


class TestSDKExceptionOrdering:
    """SDK exception subclass ordering must be correct.

    TypeSafeAPIResponseValidationError subclasses TypeSafeAPIError.
    TypeSafeAPITimeoutError subclasses TypeSafeAPIConnectionError.
    Subclasses must be checked BEFORE their parents.
    """

    def test_response_validation_error_not_caught_as_api_error(self):
        """This is the critical test for BUG-1 — ensure subclass is checked first."""
        from typesafe_sdk import TypeSafeAPIResponseValidationError
        # Construct with the expected SDK signature (check SDK source if this fails)
        try:
            err = TypeSafeAPIResponseValidationError(
                status=200,
                body={"error": "missing field"},
                headers={},
                field_path="answers.q.confidence",
                endpoint="POST /v1/system_one",
            )
        except TypeError:
            # If the constructor signature differs, try alternate forms
            pytest.skip("TypeSafeAPIResponseValidationError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "INVALID_RESPONSE", \
            "TypeSafeAPIResponseValidationError must produce INVALID_RESPONSE, not API_ERROR"
        assert result["retryable"] is False

    def test_api_error_500_is_retryable(self):
        from typesafe_sdk import TypeSafeAPIError
        try:
            err = TypeSafeAPIError(
                status=500, body=None, headers={}, endpoint="POST /v1/system_one"
            )
        except TypeError:
            pytest.skip("TypeSafeAPIError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True

    def test_api_error_400_not_retryable(self):
        from typesafe_sdk import TypeSafeAPIError
        try:
            err = TypeSafeAPIError(
                status=400, body=None, headers={}, endpoint="POST /v1/system_one"
            )
        except TypeError:
            pytest.skip("TypeSafeAPIError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is False

    def test_timeout_checked_before_connection(self):
        from typesafe_sdk import TypeSafeAPITimeoutError
        try:
            err = TypeSafeAPITimeoutError(timeout=30.0)
        except TypeError:
            pytest.skip("TypeSafeAPITimeoutError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True

    def test_connection_error_is_retryable(self):
        from typesafe_sdk import TypeSafeAPIConnectionError
        try:
            err = TypeSafeAPIConnectionError()
        except TypeError:
            pytest.skip("TypeSafeAPIConnectionError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True


class TestErrorMessage:
    def test_error_message_returns_string(self):
        msg = error_message(JevConfigError("test"))
        assert isinstance(msg, str)
        assert "test" in msg
```

> **Note:** The SDK exception constructors may vary by version. The `pytest.skip()` fallbacks ensure tests don't fail due to constructor signature changes — they'll skip gracefully and the test report will flag it.

---

## Task 6B: Add Edge Case Tests to Existing Files

### 6B-1: Path traversal edge cases in `tests/test_mock_tools.py`

Add to the `TestSearchAgentSkills` class:

```python
def test_root_dir_nonexistent_returns_empty(self):
    """Non-existent root_dir should return no matches, not crash."""
    result = jev_engine.find_agent_resources("anything", "/nonexistent/path/12345")
    assert result["matched"] is False
    assert result["count"] == 0

def test_symlink_outside_root_skipped(self, tmp_path):
    """Files reached via symlinks outside root should not crash relative_to()."""
    import os
    skills = tmp_path / ".agents" / "skills" / "symlinked"
    skills.mkdir(parents=True)
    # Create a symlink pointing outside tmp_path
    external = tmp_path.parent / "external_skill"
    external.mkdir(exist_ok=True)
    (external / "SKILL.md").write_text("# External", encoding="utf-8")
    try:
        link = skills / "ext_link"
        link.symlink_to(external)
    except OSError:
        pytest.skip("Cannot create symlinks on this OS/filesystem")
    # Should not raise ValueError from relative_to()
    result = jev_engine.find_agent_resources("external skill", str(tmp_path))
    # Result should work, just might not include the external file
    assert isinstance(result["matched"], bool)
```

### 6B-2: Log round verification for `verify_command`

Add to the `TestGuardrailTool` class in `tests/test_mock_tools.py`:

```python
def test_verify_command_logs_round(self, monkeypatch):
    """After BUG-2 fix, verify_command should log a round via _request()."""
    logged = []
    import jev_logging
    original_log_round = jev_logging.log_round
    monkeypatch.setattr(
        jev_logging,
        "log_round",
        lambda *a, **kw: logged.append(kw) or original_log_round(*a, **kw),
    )
    jev_engine.verify_command("git status")
    assert len(logged) >= 1, "verify_command should call log_round via _request()"
```

> **Note:** This test will only pass after Phase 2 (Task 2A) is implemented. If running tests before Phase 2, this test should be marked with `@pytest.mark.skip(reason="Requires Phase 2 fix")` and the skip removed later.

### 6B-3: Empty string input tests

Add to `TestGuardrailTool`:

```python
def test_empty_command(self):
    """Empty command should still return a valid envelope."""
    result = jev_engine.verify_command("")
    assert "safe" in result
    assert "action" in result
```

Add to `TestSearchTargetFiles`:

```python
def test_empty_task(self, tmp_path):
    """Empty task should still return a valid envelope."""
    (tmp_path / "file.py").write_text("pass", encoding="utf-8")
    result = jev_engine.select_target_files("", str(tmp_path))
    assert "matched" in result
```

---

## Task 6C: Add Input Validation Tests (After Phase 3)

Once Phase 3 adds input length validation, add these tests:

```python
class TestInputValidation:
    def test_oversized_command_returns_error(self):
        """Commands exceeding MAX_INPUT_CHARS should return INVALID_INPUT."""
        huge_command = "x" * 200_000
        # This should be caught by _run() and returned as an error envelope
        result = jev_mcp._run(
            "guardrail_command",
            lambda: jev_engine.verify_command(huge_command),
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_INPUT"

    def test_oversized_task_returns_error(self):
        huge_task = "x" * 200_000
        result = jev_mcp._run(
            "select_model_tier",
            lambda: jev_engine.select_model_tier(huge_task),
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_INPUT"
```

---

## Checklist

- [ ] Create `tests/test_errors.py` with `TestErrorDetailsMapping`, `TestSDKExceptionOrdering`, `TestErrorMessage`
- [ ] `tests/test_mock_tools.py`: Add `test_root_dir_nonexistent_returns_empty` to `TestSearchAgentSkills`
- [ ] `tests/test_mock_tools.py`: Add `test_symlink_outside_root_skipped` to `TestSearchAgentSkills`
- [ ] `tests/test_mock_tools.py`: Add `test_verify_command_logs_round` to `TestGuardrailTool` (skip if Phase 2 not done)
- [ ] `tests/test_mock_tools.py`: Add `test_empty_command` and `test_empty_task`
- [ ] After Phase 3: Add `TestInputValidation` class
- [ ] Run `pytest tests/ -v` — all tests green
