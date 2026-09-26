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
import re
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

#: Credential-shaped runs, matched in place. The previous implementation
#: returned "<redacted>" for the *whole field* as soon as any marker appeared
#: anywhere, which threw away the one thing a command or task line exists to
#: record, and its ">40 chars and alphanumeric" heuristic then redacted ordinary
#: long identifiers on top of that. Redacting the matched run keeps the context
#: and still covers shapes the old eight-marker list missed.
_SECRET_RE = re.compile(
    r"\b(?:sk|ts|apikey|typesafe)[-_][A-Za-z0-9_\-]{8,}"          # key-shaped token
    r"|(?:api[_-]?key|authorization|bearer)"                       # ...or a labelled credential
    r"\s*[:=]?\s*['\"]?[A-Za-z0-9._\-]{6,}",
    re.IGNORECASE,
)


def _redact(value) -> str:
    """Redact credential-shaped substrings, preserving the surrounding context.

    `curl -H 'Authorization: Bearer sk-abc123def456' https://x` keeps its
    command and its URL; only the credential disappears.
    """
    return _SECRET_RE.sub("<redacted>", str(value))


def _preview_enabled() -> bool:
    """`JEV_MCP_LOG_PREVIEW=1` restores the old full result preview."""
    try:
        from config import get_config
        return bool(get_config().log_preview)
    except Exception:
        return False


def get_logger() -> logging.Logger:
    """Return the shared jev logger, creating it lazily."""
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("jev_engine")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Keyed on the handler *type*, not on `logger.handlers` being empty: an
    # embedding host (or a test runner) can attach its own handler, and the file
    # log must not silently never be created because of it.
    installed = list(logger.handlers)
    if not any(isinstance(h, RotatingFileHandler) for h in installed):
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
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in logger.handlers
    ):
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
    """Log an MCP tool invocation (args redacted, result shape only).

    The result is deliberately **not** serialized here: the MCP runtime
    serializes it again, and a skill result carries up to 6,000 characters per
    resource. Logging its key set plus coarse sizes costs nothing and still
    answers "did the envelope look right?". Set `JEV_MCP_LOG_PREVIEW=1` to get
    the old 1,000-character preview back while debugging.
    """
    if error is not None:
        payload = {"error": error}
    elif isinstance(result, dict):
        payload = {"result_keys": sorted(result.keys())}
        for key, value in result.items():
            if isinstance(value, list) and value:
                payload[f"{key}_count"] = len(value)
            elif key == "content" and isinstance(value, str):
                payload["content_chars"] = len(value)
        if _preview_enabled():
            payload["result_preview"] = _redact(json.dumps(result, default=str)[:1000])
    else:
        payload = {"result_type": type(result).__name__}
    args_safe = {k: _redact(v) for k, v in (args or {}).items()}
    log_event("tool_call", tool=tool, args=args_safe, ms=int(ms), **payload)


def log_round(ms: int, question_keys=(), state_len: int = 0, error=None, traceback: bool = False) -> None:
    """Log one provider round (engine-level), or the failure it raised.

    The full traceback is logged once, by the caller that owns the failure
    (`jev_mcp._run`). Pass `traceback=True` only when no other layer will log
    it, otherwise an incident writes the same two kilobytes twice.
    """
    fields = {
        "ms": int(ms),
        "questions": list(question_keys),
        "state_len": int(state_len),
    }
    if error is not None:
        if traceback:
            log_exception("round_error", error, **fields)
        else:
            log_event(
                "round_error",
                error_type=type(error).__name__,
                error_message=_redact(str(error)),
                **fields,
            )
    else:
        log_event("round_ok", **fields)
