"""Environment config parsing and validation.

Borrowed from `reference/burnigtm-jev-mcp/src/config.ts` (MIT). Reads the Jev
knobs from the environment with fail-fast validation; values are re-read on
every call so tests and live deployments can toggle `JEV_MCP_MOCK` freely.
"""

import os
from dataclasses import dataclass, field

from jev_errors import JevConfigError

DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class JevConfig:
    """Parsed Jev environment configuration."""

    api_key: str = ""
    model: str = DEFAULT_MODEL
    mock: bool = False
    auto_accept: float = 0.8
    review_at: float = 0.5
    timeout_ms: int = DEFAULT_TIMEOUT_MS


def _num_env(name: str, fallback: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw.strip())
    except ValueError:
        raise JevConfigError(f"{name} must be a number between 0 and 1.")
    if not _is_finite(value) or value < 0 or value > 1:
        raise JevConfigError(f"{name} must be a number between 0 and 1.")
    return value


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _timeout_env() -> int:
    raw = os.getenv("JEV_MCP_TIMEOUT_MS")
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT_MS
    try:
        value = int(raw.strip())
    except ValueError:
        raise JevConfigError("JEV_MCP_TIMEOUT_MS must be a positive integer no greater than 2147483647.")
    if value <= 0 or value > 2_147_483_647:
        raise JevConfigError("JEV_MCP_TIMEOUT_MS must be a positive integer no greater than 2147483647.")
    return value


def _is_finite(value: float) -> bool:
    import math

    return math.isfinite(value)


def get_config() -> JevConfig:
    """Parse + validate the environment into a `JevConfig`."""
    config = JevConfig(
        api_key=(os.getenv("TYPESAFE_API_KEY") or "").strip(),
        model=(os.getenv("JEV_MCP_MODEL") or DEFAULT_MODEL).strip(),
        mock=_bool_env("JEV_MCP_MOCK"),
        auto_accept=_num_env("JEV_MCP_AUTO_ACCEPT", 0.8),
        review_at=_num_env("JEV_MCP_REVIEW_AT", 0.5),
        timeout_ms=_timeout_env(),
    )
    if config.review_at > config.auto_accept:
        raise JevConfigError("JEV_MCP_REVIEW_AT must not exceed JEV_MCP_AUTO_ACCEPT.")
    return config