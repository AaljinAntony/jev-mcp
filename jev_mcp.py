import os
import sys
import atexit
import time
import logging
from pathlib import Path

# Ensure the local directory is in Python's path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ensure_dotenv
ensure_dotenv()

from mcp.server.mcpserver import MCPServer as FastMCP

# Import tested logic from jev_engine
from jev_engine import (
    verify_command,
    find_agent_resources,
    select_target_files,
    select_model_tier as _engine_select_model_tier,
    load_jev_settings,
)
from jev_errors import JevToolError, error_details
from jev_logging import log_tool_call, log_event, log_exception, log_path
from jev_validation import _assert_finite_json

mcp = FastMCP("jev-engine")

log_event("server_start", pid=os.getpid(), log_file=str(log_path()))


def _on_shutdown() -> None:
    """Flush buffered log records and record a stop event on process exit."""
    previous = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        log_event("server_stop", pid=os.getpid())
        for handler in logging.getLogger("jev_engine").handlers:
            handler.flush()
    except Exception:
        pass
    finally:
        logging.raiseExceptions = previous


atexit.register(_on_shutdown)


def _run(tool: str, fn, **args):
    """Run a tool body, mapping typed errors to a fail-closed JSON envelope.

    Invalid responses never read as `safe:true`: validation raises before any
    policy number is produced, so the caller only ever sees the error envelope
    (plus the MCP text for debugging).
    """
    start = time.perf_counter()
    try:
        result = fn()
        _assert_finite_json(result)
        log_tool_call(tool, (time.perf_counter() - start) * 1000, args=args, result=result)
        return result
    except Exception as err:
        envelope = {"error": error_details(err)}
        ms = (time.perf_counter() - start) * 1000
        log_tool_call(tool, ms, args=args, error=envelope)
        log_exception("tool_error", err, tool=tool, args=args)
        raise JevToolError(envelope) from err


@mcp.tool()
def guardrail_command(command: str) -> dict:
    """Check whether a terminal shell command is safe to execute or potentially destructive."""
    return _run("guardrail_command", lambda: verify_command(command), command=command)


@mcp.tool()
def search_agent_skills(task: str, root_dir: str = ".") -> dict:
    """Find and retrieve relevant agent skills, workflows, and memory markdown files for a given task."""
    return _run("search_agent_skills", lambda: find_agent_resources(task=task, root_dir=root_dir), task=task, root_dir=root_dir)


@mcp.tool()
def search_target_files(task: str, root_dir: str = ".") -> dict:
    """Identify which workspace files are relevant to a task using Jev AI evaluation."""
    return _run("search_target_files", lambda: select_target_files(task=task, root_dir=root_dir), task=task, root_dir=root_dir)


@mcp.tool()
def select_model_tier(task: str) -> dict:
    """Select the optimal LLM model tier (fast, balanced, or frontier) based on task complexity."""
    return _run("select_model_tier", lambda: _engine_select_model_tier(task), task=task)


if __name__ == "__main__":
    mcp.run()