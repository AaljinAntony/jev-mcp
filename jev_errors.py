"""Typed Jev errors and envelope mapping.

Borrowed from `reference/burnigtm-jev-mcp/src/errors.ts` (MIT): a small
exception taxonomy plus `error_details()` that turns any exception into a
`{code, message, retryable}` envelope. The MCP layer serializes this envelope
so a bad decision never masquerades as `safe:true`.
"""

import json

from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeBadRequestError,
    TypeSafeError,
    TypeSafeNotFoundError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
)

try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    class ToolError(Exception):
        pass

#: Provider status codes that are worth retrying: 408 (timeout), 429 (rate
#: limit) and any 5xx (server fault).
RETRYABLE_STATUSES = frozenset({408, 429})


class JevError(Exception):
    """Base for every error this engine raises on purpose."""


class JevConfigError(JevError):
    """Bad environment / settings configuration (key missing, thresholds invalid)."""


class JevValidationError(JevError):
    """Invalid caller input (bad thresholds, negative budget, ...)."""


class JevBudgetError(JevValidationError):
    """The request exceeds the estimated context budget."""


class JevResponseError(JevError):
    """The provider returned an invalid response; no decision was accepted."""

    def __init__(self, reason: str = "TypeSafe returned an invalid response. No decision was accepted.") -> None:
        super().__init__(reason)
        self.reason = reason


class JevTimeoutError(JevError):
    """The request exceeded its configured timeout.

    An explicit message is preserved by `error_details` (the circuit breaker's
    "retry in Ns" is actionable in a way the generic advice is not); the bare
    constructor keeps the generic wording.
    """


class JevCancelledError(JevError):
    """The tool request was cancelled."""


class JevToolError(ToolError):
    """A typed Jev failure that must reach the client as isError=true.

    The JSON envelope is embedded in the message so the structured body
    survives the SDK's string-only error text.
    """

    def __init__(self, envelope: dict) -> None:
        super().__init__(json.dumps(envelope, ensure_ascii=False))
        self.envelope = envelope


def retryable_status(status: int) -> bool:
    """True for provider status codes worth another attempt: 408, 429, any 5xx."""
    return status in RETRYABLE_STATUSES or status >= 500


def error_details(err: Exception) -> dict:
    """Map any exception to a `{code, message, retryable}` envelope.

    Ordering is critical based on the SDK inheritance tree:
      TypeSafeError (base)
        ├── TypeSafeAPIError (HTTP status) -> check after ResponseValidationError & status subclasses
        │     ├── TypeSafeAPIResponseValidationError -> check BEFORE TypeSafeAPIError
        │     ├── TypeSafeAuthenticationError -> check BEFORE TypeSafeAPIError
        │     ├── TypeSafePermissionDeniedError -> check BEFORE TypeSafeAPIError
        │     ├── TypeSafeBadRequestError -> check BEFORE TypeSafeAPIError
        │     ├── TypeSafeUnprocessableEntityError -> check BEFORE TypeSafeAPIError
        │     ├── TypeSafeNotFoundError -> check BEFORE TypeSafeAPIError
        │     └── TypeSafeRateLimitError -> check BEFORE TypeSafeAPIError
        └── TypeSafeAPIConnectionError (network) -> check BEFORE TypeSafeError
              └── TypeSafeAPITimeoutError -> check BEFORE TypeSafeAPIConnectionError
    """
    if isinstance(err, JevBudgetError):
        return {"code": "INPUT_TOO_LARGE", "message": str(err), "retryable": False}
    if isinstance(err, JevValidationError):
        return {"code": "INVALID_INPUT", "message": str(err), "retryable": False}
    if isinstance(err, JevConfigError):
        return {"code": "CONFIG_ERROR", "message": str(err), "retryable": False}
    if isinstance(err, JevResponseError):
        return {"code": "INVALID_RESPONSE", "message": str(err), "retryable": False}
    if isinstance(err, JevTimeoutError):
        return {
            "code": "TIMEOUT",
            "message": str(err) or "The tool request timed out. Reduce the request size or increase JEV_MCP_TIMEOUT_MS.",
            "retryable": True,
        }
    if isinstance(err, JevCancelledError):
        return {"code": "CANCELLED", "message": "The tool request was cancelled.", "retryable": False}
    if isinstance(err, TypeSafeAPITimeoutError):
        return {
            "code": "TIMEOUT",
            "message": "The tool request timed out. Reduce the request size or increase JEV_MCP_TIMEOUT_MS.",
            "retryable": True,
        }
    if isinstance(err, TypeSafeAPIResponseValidationError):
        # field_path names the offending field and is safe; str(err) also
        # carries the endpoint URL and request id, which must not be relayed.
        field_path = getattr(err, "field_path", None)
        path_str = f" (at {field_path!r})" if field_path else ""
        return {
            "code": "INVALID_RESPONSE",
            "message": f"TypeSafe returned a response this server could not accept{path_str}.",
            "retryable": False,
        }
    if isinstance(err, TypeSafeAuthenticationError):
        return {
            "code": "AUTH_ERROR",
            "message": "TypeSafe rejected the API key. Check TYPESAFE_API_KEY.",
            "retryable": False,
        }
    if isinstance(err, TypeSafePermissionDeniedError):
        return {"code": "FORBIDDEN", "message": "TypeSafe denied access to this resource.", "retryable": False}
    if isinstance(err, TypeSafeBadRequestError):
        return {"code": "INVALID_INPUT", "message": "TypeSafe rejected the request as malformed.", "retryable": False}
    if isinstance(err, TypeSafeUnprocessableEntityError):
        return {"code": "INVALID_INPUT", "message": "TypeSafe rejected the request payload.", "retryable": False}
    if isinstance(err, TypeSafeNotFoundError):
        return {"code": "API_ERROR", "message": f"TypeSafe resource not found (HTTP {getattr(err, 'status', 404)}).", "retryable": False}
    if isinstance(err, TypeSafeRateLimitError):
        return {"code": "RATE_LIMITED", "message": "TypeSafe rate limit reached.", "retryable": True}
    # Provider errors can carry response bodies or request metadata; do not relay them.
    if isinstance(err, TypeSafeAPIError):
        status = getattr(err, "status", 500)
        return {
            "code": "API_ERROR",
            "message": f"TypeSafe API request failed (HTTP {status}).",
            "retryable": retryable_status(status),
        }
    if isinstance(err, TypeSafeAPIConnectionError):
        return {"code": "API_ERROR", "message": "Could not connect to TypeSafe.", "retryable": True}
    if isinstance(err, TypeSafeError):
        return {"code": "API_ERROR", "message": "TypeSafe could not complete the request.", "retryable": False}
    return {"code": "INTERNAL_ERROR", "message": str(err) if isinstance(err, Exception) else str(err), "retryable": False}


def error_message(err: Exception) -> str:
    """Shortcut returning just the envelope message."""
    return error_details(err)["message"]