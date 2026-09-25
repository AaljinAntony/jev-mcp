import pytest
from typesafe_sdk import (
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
)
from jev_errors import error_details
from jev_engine import get_client, _reset_client_cache


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
        err = TypeSafeAPIError(status=500, body=None, headers={}, endpoint="POST /v1/system_one")
        result = error_details(err)
        assert result["code"] == "API_ERROR"
        assert result["retryable"] is True  # 500 is retryable

    def test_timeout_is_retryable(self):
        err = TypeSafeAPITimeoutError(timeout=30.0)
        result = error_details(err)
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True

    def test_connection_error_is_retryable(self):
        err = TypeSafeAPIConnectionError()
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
