"""Environment config parsing and validation.

Borrowed from `reference/burnigtm-jev-mcp/src/config.ts` (MIT). Reads the Jev
knobs from the environment with fail-fast validation. The parsed result is
cached; call `_reset_config_cache()` after changing the environment so tests and
live deployments can toggle `JEV_MCP_MOCK` freely.
"""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jev_errors import JevConfigError

DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN_S = 30.0
DEFAULT_AUTH_COOLDOWN_S = 300.0


def ensure_dotenv() -> bool:
    """Load the repo `.env` as a *fallback* only. Returns True if a file was loaded.

    An injected value always wins over `.env` — a stale `.env` must never
    override the authoritative MCP `environment` block, least of all
    JEV_MCP_MOCK, which would silently replace live decisions with the mock
    judge.

    A *blank* injected value is the one exception, and it is not a corner case:
    opencode resolves `"TYPESAFE_API_KEY": "{env:TYPESAFE_API_KEY}"` to `""`
    whenever that variable is missing from its own environment, and hands the
    empty string to the child. `load_dotenv(override=False)` treats "already
    present" as authoritative even for an empty value, so the `.env` key could
    never take effect and every live tool call failed with
    "TYPESAFE_API_KEY environment variable is not configured" — in every
    workspace, since the file is found relative to this module, not the CWD.

    So a variable is only "already set" when it holds something other than
    whitespace.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return False
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return False   # never search upward from an unrelated CWD
    try:
        values = dotenv_values(dotenv_path=env_path)
    except OSError:
        return False
    applied = False
    for key, value in values.items():
        if value is None:
            continue
        if (os.environ.get(key) or "").strip():
            continue    # a real injected value stays authoritative
        os.environ[key] = value
        applied = True
    return applied


@dataclass(frozen=True)
class JevConfig:
    """Parsed Jev environment configuration."""

    api_key: str = ""
    model: str = DEFAULT_MODEL
    mock: bool = False
    auto_accept: float = 0.8
    review_at: float = 0.5
    #: Total per-tool-call budget in ms, *including* every retry and its backoff.
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    #: Extra directories an LLM-supplied `root_dir` may resolve inside. Empty
    #: means "the process CWD and its ancestors only".
    allowed_roots: tuple = ()
    #: Consecutive retryable provider failures before the breaker opens.
    breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD
    #: Seconds the breaker stays open after a retryable failure.
    breaker_cooldown_s: float = DEFAULT_BREAKER_COOLDOWN_S
    #: Seconds the breaker stays open after an auth failure; a bad key does not
    #: fix itself in 30 seconds.
    auth_cooldown_s: float = DEFAULT_AUTH_COOLDOWN_S
    #: Opt-in: log a 1,000-character preview of every tool result again. Off by
    #: default because the result is serialized twice (here and by the MCP
    #: runtime) and a skill result carries kilobytes of file content.
    log_preview: bool = False


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
    """The TOTAL per-tool-call budget, not a per-attempt timeout.

    The engine passes this as both `RetryPolicy.timeout` (the whole budget) and
    the per-attempt clamp, so retries and their backoff fit inside it.
    """
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
    return math.isfinite(value)


def _positive_int_env(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = int(raw.strip())
    except ValueError:
        raise JevConfigError(f"{name} must be a positive integer no greater than 2147483647.")
    if value <= 0 or value > 2_147_483_647:
        raise JevConfigError(f"{name} must be a positive integer no greater than 2147483647.")
    return value


def _positive_float_env(name: str, fallback: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw.strip())
    except ValueError:
        raise JevConfigError(f"{name} must be a positive number of seconds.")
    if not _is_finite(value) or value <= 0:
        raise JevConfigError(f"{name} must be a positive number of seconds.")
    return value


def _roots_env() -> tuple:
    """Parse JEV_MCP_ALLOWED_ROOTS into resolved absolute paths.

    `os.pathsep`-separated (`;` on Windows, `:` on POSIX) so a single knob can
    name several roots. Relative entries are dropped rather than resolved
    against a CWD the caller may not control, and a path that does not exist is
    kept only if it can be resolved — `_allowed_roots` filters on `is_dir()`.
    """
    raw = (os.getenv("JEV_MCP_ALLOWED_ROOTS") or "").strip()
    if not raw:
        return ()
    out = []
    for part in raw.split(os.pathsep):
        piece = part.strip()
        if not piece:
            continue
        try:
            p = Path(piece).expanduser()
            if p.is_absolute():
                out.append(str(p.resolve()))
        except (OSError, RuntimeError, ValueError):
            # An unresolvable entry (e.g. a symlink loop) is skipped, not fatal.
            continue
    return tuple(dict.fromkeys(out))


_cached_config: Optional[JevConfig] = None


def _reset_config_cache() -> None:
    """Clear the cached config. Exposed for tests."""
    global _cached_config
    _cached_config = None


def get_config() -> JevConfig:
    """Parse + validate the environment into a cached `JevConfig`."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    config = JevConfig(
        api_key=(os.getenv("TYPESAFE_API_KEY") or "").strip(),
        model=(os.getenv("JEV_MCP_MODEL") or DEFAULT_MODEL).strip(),
        mock=_bool_env("JEV_MCP_MOCK"),
        auto_accept=_num_env("JEV_MCP_AUTO_ACCEPT", 0.8),
        review_at=_num_env("JEV_MCP_REVIEW_AT", 0.5),
        timeout_ms=_timeout_env(),
        allowed_roots=_roots_env(),
        breaker_threshold=_positive_int_env("JEV_MCP_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD),
        breaker_cooldown_s=_positive_float_env("JEV_MCP_BREAKER_COOLDOWN_S", DEFAULT_BREAKER_COOLDOWN_S),
        auth_cooldown_s=_positive_float_env("JEV_MCP_AUTH_COOLDOWN_S", DEFAULT_AUTH_COOLDOWN_S),
        log_preview=_bool_env("JEV_MCP_LOG_PREVIEW"),
    )
    if config.review_at > config.auto_accept:
        raise JevConfigError("JEV_MCP_REVIEW_AT must not exceed JEV_MCP_AUTO_ACCEPT.")

    _cached_config = config
    return config