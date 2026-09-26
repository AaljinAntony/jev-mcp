"""Transport-level tests: the real `jev_mcp.py` server over real stdio JSON-RPC.

Every other suite calls the engine's Python functions directly, which cannot see
the three things that only exist between the process boundary and the tool body:
the MCP handshake, the `tools/list` advertisement, and whether a fail-closed
error actually arrives as `isError: true` rather than as a cheerful result.

These spawn the server exactly the way opencode does (`python jev_mcp.py` with
newline-delimited JSON-RPC on stdin/stdout) and run in mock mode, so they need
no `TYPESAFE_API_KEY` and no network.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "jev_mcp.py"
PYTHON = Path(sys.executable)

#: Long enough for a cold interpreter start plus four tool calls, short enough
#: that a hang is a test failure rather than a stuck suite.
BOOT_TIMEOUT_S = 45


class StdioClient:
    """Newline-delimited JSON-RPC 2.0 over a child process's stdin/stdout."""

    def __init__(self, proc):
        self.proc = proc
        self._next_id = 1

    def request(self, method, params=None, timeout_s=BOOT_TIMEOUT_S):
        message = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params if params is not None else {},
        }
        self._next_id += 1
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"no response to {method}; the server exited or closed stdout")
        return json.loads(line)

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _spawn(tmp_path, env_extra=None, cwd=None):
    env = dict(os.environ)
    env["JEV_MCP_MOCK"] = "1"          # no key, no network, deterministic
    env["JEV_MCP_LOG_FILE"] = str(tmp_path / "jev_engine.log")
    env["JEV_MCP_LOG_PREVIEW"] = "0"
    # This process's CWD is the repo, so the server's allowlist is the repo and
    # its ancestors. `tmp_path` lives under the system temp dir, which is
    # therefore granted explicitly — the same grant tests/conftest.py makes.
    env["JEV_MCP_ALLOWED_ROOTS"] = tempfile.gettempdir()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [str(PYTHON), str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd or REPO),
        env=env,
        encoding="utf-8",
    )
    # Drain stderr continuously. The server logs to stderr as well as to its
    # file, and an undrained pipe fills at ~64 KB and then deadlocks the server
    # mid-call — which shows up here as a hung test, not as an obvious failure.
    stderr_tail = []

    def _drain():
        for line in proc.stderr:
            stderr_tail.append(line)
            del stderr_tail[:-40]

    threading.Thread(target=_drain, daemon=True).start()
    client = StdioClient(proc)
    client.stderr_tail = stderr_tail
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test_mcp_transport", "version": "1"},
        },
    )
    assert "result" in init, f"initialize failed: {init}"
    client.notify("notifications/initialized")
    return client


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    client = _spawn(tmp_path_factory.mktemp("transport"))
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def tools(server):
    result = server.request("tools/list")
    assert "result" in result, f"tools/list failed: {result}"
    return {t["name"]: t for t in result["result"]["tools"]}


@pytest.fixture(scope="module")
def routing_server(tmp_path_factory):
    """A server whose CWD is a project with model routing switched on.

    Settings are discovered from the process CWD, so this is the only way to
    reach the enabled branch without editing the repo's own
    `jevs_settings.json`.
    """
    project = tmp_path_factory.mktemp("routing-project")
    (project / "jevs_settings.json").write_text(
        json.dumps(
            {
                "enable_model_routing": True,
                "models": {
                    "fast": "vendor/mock-fast",
                    "balanced": "vendor/mock-balanced",
                    "frontier": "vendor/mock-frontier",
                },
                "scan_paths": [".agents/skills"],
            }
        ),
        encoding="utf-8",
    )
    client = _spawn(tmp_path_factory.mktemp("routing-log"), cwd=project)
    try:
        yield client
    finally:
        client.close()


def _call(client, name, arguments):
    response = client.request("tools/call", {"name": name, "arguments": arguments})
    assert "result" in response, f"tools/call {name} returned no result: {response}"
    return response["result"]


def _body(result):
    """The tool's JSON payload, unwrapped from the MCP `Error executing tool` prefix."""
    content = result.get("content", [])
    text = content[0].get("text", "") if content else ""
    if text.startswith("Error executing tool ") and ": {" in text:
        text = text[text.index(": {") + 2:]
    return json.loads(text)


# 1. The advertisement
def test_all_four_tools_are_advertised(tools):
    assert set(tools) == {
        "guardrail_command",
        "search_agent_skills",
        "search_target_files",
        "select_model_tier",
    }
    for name, spec in tools.items():
        assert spec.get("description"), f"{name} has no description for the model"
        assert "inputSchema" in spec, f"{name} has no input schema"


# 2. A round trip per tool
def test_guardrail_command_round_trip(server):
    result = _call(server, "guardrail_command", {"command": "git status"})
    assert result.get("isError") is not True
    body = _body(result)
    assert body["safe"] is True
    assert body["action"] in {"auto", "review", "escalate"}
    assert body["model"].endswith("+mock")


def test_search_target_files_round_trip(server, tmp_path):
    (tmp_path / "widget.py").write_text("x = 1", encoding="utf-8")
    result = _call(
        server, "search_target_files", {"task": "find the widget", "root_dir": str(tmp_path)}
    )
    body = _body(result)
    assert "ranked" in body and "candidates_truncated" in body
    assert body["action"] in {"auto", "review", "escalate"}


def test_search_agent_skills_round_trip(server, tmp_path):
    skills = tmp_path / ".agents" / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\ndescription: release notes drafting\n---\n# demo\n", encoding="utf-8"
    )
    result = _call(
        server, "search_agent_skills", {"task": "draft release notes", "root_dir": str(tmp_path)}
    )
    body = _body(result)
    assert body["candidates_considered"] == 1
    assert isinstance(body["resources"], list)


def test_select_model_tier_round_trip(server):
    """With routing off (the shipped default) the tool reports the gate itself."""
    result = _call(server, "select_model_tier", {"task": "refactor the auth module"})
    body = _body(result)
    assert body["enabled"] is False
    assert body["recommended_tier"] is None
    assert body["action"] == "review"
    assert isinstance(body["model_map"], dict)


def test_select_model_tier_with_routing_enabled(routing_server):
    """With routing on, the same call returns a tier and the configured model."""
    result = _call(routing_server, "select_model_tier", {"task": "refactor the auth module"})
    body = _body(result)
    assert body["enabled"] is True
    assert body["recommended_tier"] in {"fast", "balanced", "frontier"}
    # The model reported must be the configured model *for that tier*, not a
    # fixed one — the mapping is the whole point of the tool.
    assert body["recommended_model"] == f"vendor/mock-{body['recommended_tier']}"
    assert body["action"] in {"auto", "review", "escalate"}


# 3. Fail-closed across the transport
def test_a_rejected_root_dir_arrives_as_iserror(server):
    """A drive root is refused in-process; the refusal must survive the wire.

    If the server returned the error envelope as an ordinary result, an agent
    would read `{"error": ...}` as a decision and carry on.
    """
    result = _call(server, "search_target_files", {"task": "x", "root_dir": "C:\\Users"})
    assert result.get("isError") is True
    body = _body(result)
    assert "error" in body and "code" in body["error"]
    assert body["error"]["code"] in {"INVALID_ROOT", "INVALID_INPUT", "VALIDATION_ERROR"}


def test_a_sibling_prefix_escape_arrives_as_iserror(server):
    """`<allowed-root>-evil` shares a textual prefix but is not inside it.

    The allowlist grants the system temp dir; this is its sibling. A
    `startswith` check would wave it through, which is exactly the escape
    `_is_within` exists to refuse.
    """
    granted = Path(tempfile.gettempdir()).resolve()
    evil = Path(str(granted).rstrip("\\/") + "-evil")
    evil.mkdir(parents=True, exist_ok=True)
    try:
        result = _call(server, "search_target_files", {"task": "x", "root_dir": str(evil)})
        assert result.get("isError") is True
        assert "outside the allowed roots" in _body(result)["error"]["message"]
    finally:
        import shutil

        shutil.rmtree(evil, ignore_errors=True)


def test_error_results_never_carry_a_safety_verdict(server):
    result = _call(server, "guardrail_command", {"command": "x" * 200_000})
    assert result.get("isError") is True
    body = _body(result)
    assert "safe" not in body, "a rejected input was reported as a safety decision"
