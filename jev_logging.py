"""Filesystem logging for the jev-engine MCP server and CLI.

Writes JSON-ish single-line events to ``<repo>/logs/jev_engine.log`` (an
absolute, cwd-independent path) so failures in any workspace leave a trace.
Override the destination with the ``JEV_MCP_LOG_FILE`` env var.

Every function here degrades silently: a read-only or unwritable log directory
must never crash the server.
"""

import json
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def log_path() -> Path:
    override = os.getenv("JEV_MCP_LOG_FILE")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "logs" / "jev_engine.log"


_logger: logging.Logger | None = None

#: Values that look like credentials and must never be written to the log.
_SECRET_MARKERS = (
    "sk-",
    "ts_",
    "apikey_",
    "api_key=",
    "api_key:",
    "typesafe_api_key",
    "bearer ",
    "authorization:",
)


def _redact(value) -> str:
    text = str(value)
    lowered = text.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            return "<redacted>"
    # Heuristic: long alphanumeric strings that look like API keys
    if len(text) > 40 and text.replace("-", "").replace("_", "").isalnum():
        return "<redacted>"
    return text


def get_logger() -> logging.Logger:
    """Return the shared jev logger, creating it lazily."""
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("jev_engine")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            path = log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
            logger.addHandler(handler)
        except Exception:
            pass
        try:
            console = logging.StreamHandler(sys.stderr)
            console.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
            logger.addHandler(console)
        except Exception:
            pass
    _logger = logger
    return logger


def log_event(event: str, **fields) -> None:
    """Emit one JSON single-line log event with the given fields."""
    record = {"evt": event}
    record.update(fields)
    get_logger().info(json.dumps(record, default=str, ensure_ascii=False))


def log_exception(event: str, err: BaseException, **fields) -> None:
    """Emit an event that includes a compacted traceback for `err`."""
    fields.setdefault("error_type", type(err).__name__)
    fields.setdefault("error_message", _redact(str(err)))
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    fields["traceback"] = tb[-2000:] if tb else None
    log_event(event, **fields)


def log_tool_call(tool: str, ms: int, args=None, result=None, error=None) -> None:
    """Log an MCP tool invocation (args/value serialized, secrets redacted)."""
    if error is not None:
        payload = {"error": error}
    else:
        dumped = json.dumps(result, default=str)
        payload = {
            "result_len": len(dumped),
            "result_preview": dumped[:1000],
        }
    args_safe = {k: _redact(v) for k, v in (args or {}).items()}
    log_event("tool_call", tool=tool, args=args_safe, ms=int(ms), **payload)


def log_round(ms: int, question_keys=(), state_len: int = 0, error=None) -> None:
    """Log one provider round (engine-level), or the failure it raised."""
    fields = {
        "ms": int(ms),
        "questions": list(question_keys),
        "state_len": int(state_len),
    }
    if error is not None:
        log_exception("round_error", error, **fields)
    else:
        log_event("round_ok", **fields)