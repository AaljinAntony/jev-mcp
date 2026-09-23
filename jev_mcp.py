import os
import sys
from pathlib import Path

# Load environment variables
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        load_dotenv(override=True)
except ImportError:
    pass

from mcp.server.mcpserver import MCPServer as FastMCP

# Ensure the local directory is in Python's path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import tested logic from jev_engine
from jev_engine import (
    verify_command,
    find_agent_resources,
    select_target_files,
    select_model_tier as _engine_select_model_tier,
    load_jev_settings,
)
from jev_errors import error_details

mcp = FastMCP("jev-engine")


def _run(fn):
    """Run a tool body, mapping typed errors to a fail-closed JSON envelope.

    Invalid responses never read as `safe:true`: validation raises before any
    policy number is produced, so the caller only ever sees the error envelope
    (plus the MCP text for debugging).
    """
    try:
        return fn()
    except Exception as err:
        return {"error": error_details(err)}


@mcp.tool()
def guardrail_command(command: str) -> dict:
    """Check whether a terminal shell command is safe to execute or potentially destructive."""
    return _run(lambda: verify_command(command))


@mcp.tool()
def search_agent_skills(task: str, root_dir: str = ".") -> dict:
    """Find and retrieve relevant agent skills, workflows, and memory markdown files for a given task."""
    return _run(lambda: find_agent_resources(task=task, root_dir=root_dir))


@mcp.tool()
def search_target_files(task: str, root_dir: str = ".") -> dict:
    """Identify which workspace files are relevant to a task using Jev AI evaluation."""
    return _run(lambda: select_target_files(task=task, root_dir=root_dir))


@mcp.tool()
def select_model_tier(task: str) -> dict:
    """Select the optimal LLM model tier (fast, balanced, or frontier) based on task complexity."""
    return _run(lambda: _engine_select_model_tier(task))


if __name__ == "__main__":
    mcp.run()