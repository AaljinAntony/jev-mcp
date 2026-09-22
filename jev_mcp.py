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
    get_client,
    load_jev_settings,
    Choice,
    execute_system_one,
    get_answer,
    get_val,
)

mcp = FastMCP("jev-engine")


@mcp.tool()
def guardrail_command(command: str) -> dict:
    """Check whether a terminal shell command is safe to execute or potentially destructive."""
    return verify_command(command)


@mcp.tool()
def search_agent_skills(task: str, root_dir: str = ".") -> dict:
    """Find and retrieve relevant agent skills, workflows, and memory markdown files for a given task."""
    return find_agent_resources(task=task, root_dir=root_dir)


@mcp.tool()
def search_target_files(task: str, root_dir: str = ".") -> dict:
    """Identify which workspace files are relevant to a task using Jev AI evaluation."""
    return select_target_files(task=task, root_dir=root_dir)


@mcp.tool()
def select_model_tier(task: str) -> dict:
    """Select the optimal LLM model tier (fast, balanced, or frontier) based on task complexity."""
    settings = load_jev_settings()
    payload = {
        "enabled": settings["enable_model_routing"],
        "task": task,
        "recommended_tier": None,
        "recommended_model": None,
        "model_map": settings["models"],
    }
    if not settings["enable_model_routing"]:
        return payload

    client = get_client()
    res = execute_system_one(
        client,
        state=f"User Task: {task}\nDetermine the appropriate model tier based on task complexity and reasoning required.",
        questions={
            "tier": Choice(
                criteria={
                    "fast": "Typos, simple lookups, docstrings, boilerplate",
                    "balanced": "Standard bugs, test cases, isolated feature changes",
                    "frontier": "Multi-file refactors, architecture design, complex algorithmic logic",
                },
                instructions="Select the appropriate model capability tier for this task."
            )
        }
    )
    selected_tier = get_val(get_answer(res, "tier")) or "balanced"
    payload["recommended_tier"] = selected_tier
    payload["recommended_model"] = settings["models"].get(selected_tier)
    return payload


if __name__ == "__main__":
    mcp.run()