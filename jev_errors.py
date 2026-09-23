"""Typed Jev errors and envelope mapping.

Borrowed from `reference/burnigtm-jev-mcp/src/errors.ts` (MIT): a small
exception taxonomy plus `error_details()` that turns any exception into a
`{code, message, retryable}` envelope. The MCP layer serializes this envelope
so a bad decision never masquerades as `safe:true`.
"""

from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeError,
)

#: Provider status codes that are worth retrying: 408 (timeout), 429 (rate
#: limit) and any 5xx (server fault).
RETRYABLE_STATUSES = frozenset({408, 429})


class JevConfigError(Exception):
    """Bad environment / settings configuration (key missing, thresholds invalid)."""


class JevValidationError(Exception):
    """Invalid caller input (bad thresholds, negative budget, ...)."""


class JevBudgetError(JevValidationError):
    """The request exceeds the estimated context budget."""


class JevResponseError(Exception):
    """The provider returned an invalid response; no decision was accepted."""

    def __init__(self, reason: str = "TypeSafe returned an invalid response. No decision was accepted.") -> None:
        super().__init__(reason)
        self.reason = reason


class JevTimeoutError(Exception):
    """The request exceeded its configured timeout."""


class JevCancelledError(Exception):
    """The tool request was cancelled."""


def _retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES or status >= 500


def error_details(err: Exception) -> dict:
    """Map any exception to a `{code, message, retryable}` envelope.

    Ordering matters: `TypeSafeAPITimeoutError` subclasses
    `TypeSafeAPIConnectionError` and `TimeoutError`, so it must be checked
    before the generic connection branch.
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
            "message": "The tool request timed out. Reduce the request size or increase JEV_MCP_TIMEOUT_MS.",
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
    # Provider errors can carry response bodies or request metadata; do not relay them.
    if isinstance(err, TypeSafeAPIError):
        return {
            "code": "API_ERROR",
            "message": f"TypeSafe API request failed (HTTP {err.status}).",
            "retryable": _retryable_status(err.status),
        }
    if isinstance(err, TypeSafeAPIResponseValidationError):
        return {"code": "INVALID_RESPONSE", "message": str(err), "retryable": False}
    if isinstance(err, TypeSafeAPIConnectionError):
        return {"code": "API_ERROR", "message": "Could not connect to TypeSafe.", "retryable": True}
    if isinstance(err, TypeSafeError):
        return {"code": "API_ERROR", "message": "TypeSafe could not complete the request.", "retryable": False}
    return {"code": "INTERNAL_ERROR", "message": str(err) if isinstance(err, Exception) else str(err), "retryable": False}


def error_message(err: Exception) -> str:
    """Shortcut returning just the envelope message."""
    return error_details(err)["message"]