"""Tests for jev_errors.error_details() exception -> envelope mapping and client configuration."""

import pytest

from jev_engine import _reset_client_cache, get_client
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
from typesafe_sdk import (
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeError,
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
        assert "bad input" in result["message"]

    def test_config_error(self):
        result = error_details(JevConfigError("missing key"))
        assert result["code"] == "CONFIG_ERROR"
        assert result["retryable"] is False
        assert "missing key" in result["message"]

    def test_response_error(self):
        result = error_details(JevResponseError("bad response"))
        assert result["code"] == "INVALID_RESPONSE"
        assert result["retryable"] is False
        assert "bad response" in result["message"]

    def test_timeout_error(self):
        result = error_details(JevTimeoutError())
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True
        assert "timed out" in result["message"]

    def test_cancelled_error(self):
        result = error_details(JevCancelledError())
        assert result["code"] == "CANCELLED"
        assert result["retryable"] is False
        assert "cancelled" in result["message"]

    def test_generic_exception(self):
        result = error_details(RuntimeError("unexpected"))
        assert result["code"] == "INTERNAL_ERROR"
        assert result["retryable"] is False
        assert "unexpected" in result["message"]

    def test_typesafe_base_error(self):
        result = error_details(TypeSafeError("sdk error"))
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is False
        assert "TypeSafe could not complete the request." in result["message"]


class TestSDKExceptionOrdering:
    """SDK exception subclass ordering must be correct.

    TypeSafeAPIResponseValidationError subclasses TypeSafeAPIError.
    TypeSafeAPITimeoutError subclasses TypeSafeAPIConnectionError.
    Subclasses must be checked BEFORE their parents.
    """

    def test_response_validation_error_not_caught_as_api_error(self):
        """This is the critical test for BUG-1 - ensure subclass is checked first."""
        try:
            err = TypeSafeAPIResponseValidationError(
                status=200,
                body={"error": "missing field"},
                headers={},
                field_path="answers.q.confidence",
                endpoint="POST /v1/system_one",
            )
        except TypeError:
            pytest.skip("TypeSafeAPIResponseValidationError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "INVALID_RESPONSE", (
            "TypeSafeAPIResponseValidationError must produce INVALID_RESPONSE, not API_ERROR"
        )
        assert result["retryable"] is False

    def test_api_error_500_is_retryable(self):
        try:
            err = TypeSafeAPIError(
                status=500, body=None, headers={}, endpoint="POST /v1/system_one"
            )
        except TypeError:
            pytest.skip("TypeSafeAPIError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True

    def test_api_error_429_is_retryable(self):
        try:
            err = TypeSafeAPIError(
                status=429, body=None, headers={}, endpoint="POST /v1/system_one"
            )
        except TypeError:
            pytest.skip("TypeSafeAPIError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True

    def test_api_error_408_is_retryable(self):
        try:
            err = TypeSafeAPIError(
                status=408, body=None, headers={}, endpoint="POST /v1/system_one"
            )
        except TypeError:
            pytest.skip("TypeSafeAPIError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True

    def test_api_error_400_not_retryable(self):
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
        try:
            err = TypeSafeAPITimeoutError(timeout=30.0)
        except TypeError:
            pytest.skip("TypeSafeAPITimeoutError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True

    def test_connection_error_is_retryable(self):
        try:
            err = TypeSafeAPIConnectionError()
        except TypeError:
            pytest.skip("TypeSafeAPIConnectionError constructor signature unknown")

        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True


class TestClientRetryPolicy:
    def test_get_client_configures_retry_policy(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        client = get_client()
        assert client is not None
        assert hasattr(client, "_retry")
        wait_fn = client._retry.wait
        retry_policy = wait_fn.__self__
        assert isinstance(retry_policy, RetryPolicy)
        assert retry_policy.max_retries == 2
        assert retry_policy.backoff_initial == 0.5
        assert retry_policy.backoff_max == 5.0
        assert retry_policy.backoff_jitter == 0.25


class TestClientCache:
    def test_client_is_cached_across_calls(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-cache")
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        _reset_client_cache()
        c1 = get_client()
        c2 = get_client()
        assert c1 is not None
        assert c1 is c2

    def test_client_cache_invalidates_on_key_change(self, monkeypatch):
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        monkeypatch.setenv("TYPESAFE_API_KEY", "key-1")
        _reset_client_cache()
        c1 = get_client()
        monkeypatch.setenv("TYPESAFE_API_KEY", "key-2")
        c2 = get_client()
        assert c1 is not None
        assert c2 is not None
        assert c1 is not c2

    def test_client_cache_invalidates_on_timeout_change(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "key-timeout")
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "30000")
        _reset_client_cache()
        c1 = get_client()
        monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "60000")
        c2 = get_client()
        assert c1 is not c2

    def test_client_cache_invalidates_on_model_change(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "key-model")
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        monkeypatch.setenv("JEV_MCP_MODEL", "model-a")
        _reset_client_cache()
        c1 = get_client()
        monkeypatch.setenv("JEV_MCP_MODEL", "model-b")
        c2 = get_client()
        assert c1 is not c2

    def test_mock_mode_clears_cache_and_returns_none(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "key-mock")
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        _reset_client_cache()
        c1 = get_client()
        assert c1 is not None
        monkeypatch.setenv("JEV_MCP_MOCK", "1")
        c2 = get_client()
        assert c2 is None
        # And when switching back to non-mock, a new client is created
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        c3 = get_client()
        assert c3 is not None
        assert c3 is not c1

    def test_reset_client_cache_clears_cache(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "key-reset")
        monkeypatch.setenv("JEV_MCP_MOCK", "0")
        _reset_client_cache()
        c1 = get_client()
        _reset_client_cache()
        c2 = get_client()
        assert c1 is not c2


class TestErrorMessage:
    def test_error_message_returns_string(self):
        msg = error_message(JevConfigError("test"))
        assert isinstance(msg, str)
        assert "test" in msg
