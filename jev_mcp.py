import os
import sys
import json
import atexit
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure the local directory is in Python's path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ensure_dotenv
ensure_dotenv()

from mcp.server.mcpserver import MCPServer as FastMCP

# Import tested logic from jev_engine
from jev_engine import (
    SERVER_NAME,
    verify_command,
    find_agent_resources,
    select_target_files,
    select_mcp_tools as _engine_select_mcp_tools,
    select_model_tier as _engine_select_model_tier,
)
from jev_errors import JevToolError, error_details
from jev_logging import log_tool_call, log_event, log_exception, log_path
from jev_validation import _assert_finite_json

mcp = FastMCP(SERVER_NAME)

log_event("server_start", pid=os.getpid(), log_file=str(log_path()), server=SERVER_NAME)


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
def search_agent_skills(task: str, root_dir: str = ".", task_file: str = "") -> dict:
    """Find and retrieve relevant agent skills, workflows, and memory markdown files for a given task.

    `task_file` is an optional path to a file holding the real prompt (a saved
    prompt or a plan); its head is read and judged alongside `task`. It is
    confined to the same allowlist as `root_dir`.
    """
    return _run("search_agent_skills", lambda: find_agent_resources(task=task, root_dir=root_dir, task_file=task_file or None), task=task, root_dir=root_dir, task_file=task_file or None)


@mcp.tool()
def search_target_files(task: str, root_dir: str = ".", task_file: str = "") -> dict:
    """Identify which workspace files are relevant to a task using Jev AI evaluation.

    `task_file` is an optional path to a file holding the real prompt; it is
    confined to the same allowlist as `root_dir`.
    """
    return _run("search_target_files", lambda: select_target_files(task=task, root_dir=root_dir, task_file=task_file or None), task=task, root_dir=root_dir, task_file=task_file or None)


@mcp.tool()
def select_model_tier(task: str) -> dict:
    """Select the optimal LLM model tier (fast, balanced, or frontier) based on task complexity."""
    return _run("select_model_tier", lambda: _engine_select_model_tier(task), task=task)


def _mcp_log_summary(mcps) -> str:
    """A one-line, bounded description of the caller's MCP roster for the log.

    The raw list is never logged: an agent can send a few hundred tools and the
    log line is written on every call, with the tool schema repeated once per
    tool. Names alone answer "which roster was judged".
    """
    entries = mcps
    if isinstance(entries, str):
        try:
            entries = json.loads(entries)
        except (ValueError, TypeError):
            return "unparseable json"
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return f"unexpected type {type(mcps).__name__}"
    names = [
        str(entry.get("name", "?"))[:40]
        for entry in entries[:64]
        if isinstance(entry, dict)
    ]
    return f"{len(entries)} servers: {', '.join(names)}"


@mcp.tool()
def select_mcp_tools(
    task: str,
    mcps: Optional[List[Dict[str, Any]]] = None,
    mcps_json: str = "",
    root_dir: str = ".",
    task_file: str = "",
    max_tools: int = 20,
) -> dict:
    """Choose which MCP server, and which of its tools, fits the current task.

    Pass the roster of MCP servers you are connected to as `mcps`:
    `[{"name": ..., "description": ..., "tools": [{"name": ..., "description": ...}]}]`.
    If your client cannot send an array parameter, send the same JSON as
    `mcps_json` instead. Pass every server you are connected to: the judge can
    only choose from what it is given, and it never recommends itself.

    Read `exists` before acting on the result:
      * `answered`  - one server is the right one; use `primary` and the `tools` list.
      * `ambiguous` - several servers are comparably usable; `servers` lists them
        and no tool is chosen for you. Decide, or ask.
      * `absent` / `partial` - no supplied server has a tool this task needs; proceed
        with your own built-in tools.
      * `no_candidates` - nothing was left to choose from (`excluded` says why).

    `action` is `auto` / `review` / `escalate`; below `auto` treat the selection as
    a hint rather than an instruction.
    """
    roster = mcps if mcps else (mcps_json or None)
    return _run(
        "select_mcp_tools",
        lambda: _engine_select_mcp_tools(
            task=task,
            mcps=roster,
            root_dir=root_dir,
            task_file=task_file or None,
            max_tools=max_tools,
        ),
        task=task,
        mcps=_mcp_log_summary(roster),
        root_dir=root_dir,
        task_file=task_file or None,
        max_tools=max_tools,
    )


if __name__ == "__main__":
    mcp.run()