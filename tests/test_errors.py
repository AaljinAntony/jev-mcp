import pytest
from typesafe_sdk import (
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
)
from jev_errors import error_details
from jev_engine import get_client


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
