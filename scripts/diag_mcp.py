"""Transport-level MCP diagnostic client for jev-engine.

Spawns ``jev_mcp.py`` over stdio (the same way opencode does) and runs one tool
call, printing the raw JSON-RPC response plus the server's stderr. Run it from
any workspace to reproduce a tool failure with a specific ``root_dir`` and task.

Examples::

    python scripts/diag_mcp.py --tool guardrail_command --command "git status"
    python scripts/diag_mcp.py --tool search_agent_skills --task "find skills for ui bug fix" --root_dir /path/to/your/workspace
    python scripts/diag_mcp.py --tool search_target_files --task "find config" --root_dir /path/to/your/workspace --mock
    python scripts/diag_mcp.py --tool select_mcp_tools --task "commit the staged changes" --mcps '[{"name":"git","tools":[{"name":"git_commit","description":"create a commit"}]}]' --mock

Relevant env overrides: ``JEV_DIAG_PYTHON``, ``JEV_DIAG_SERVER``.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _default_python() -> str:
    """Resolve the interpreter to launch, in order of decreasing trust.

    The venv layout is platform-specific (`.venv\\Scripts\\python.exe` on Windows,
    `.venv/bin/python` elsewhere), so probing both beats hardcoding one: this
    script is the documented troubleshooting entry point and it has to run
    wherever the server runs.
    """
    candidates = [
        REPO / ".venv" / "Scripts" / "python.exe",
        REPO / ".venv" / "bin" / "python",
        REPO / ".venv" / "bin" / "python3",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    # `sys.executable` is the interpreter running this script, which is already
    # the venv one whenever the script was launched correctly. The bare name is
    # the last resort so PATH resolution can still find something.
    return sys.executable or "python"


PYTHON = os.environ.get("JEV_DIAG_PYTHON") or _default_python()
SERVER = os.environ.get("JEV_DIAG_SERVER") or str(REPO / "jev_mcp.py")


def rpc(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line) if line else None


def build_args(args):
    if args.tool == "guardrail_command":
        return {"command": args.command}
    if args.tool == "select_model_tier":
        return {"task": args.task}
    if args.tool == "select_mcp_tools":
        # The roster is only available on the command line for this tool, so it
        # arrives as JSON. `--mcps @path` reads it from a file, which is the
        # form that survives PowerShell: a JSON literal typed inline loses its
        # double quotes on the way to a native executable.
        # `mcps` is tried first because it is the shape a real agent sends; a
        # client that can only pass scalars uses `mcps_json`.
        if not args.mcps:
            raise SystemExit("--mcps is required for select_mcp_tools (a JSON list of servers, or @file)")
        raw = args.mcps
        if raw.startswith("@"):
            try:
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            except OSError as exc:
                raise SystemExit(f"--mcps file could not be read: {exc}")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise SystemExit(f"--mcps is not valid JSON: {exc}")
        if isinstance(parsed, list):
            return {"task": args.task, "mcps": parsed, "root_dir": args.root_dir}
        return {"task": args.task, "mcps_json": raw, "root_dir": args.root_dir}
    if args.tool in ("search_agent_skills", "search_target_files"):
        return {"task": args.task, "root_dir": args.root_dir}
    raise SystemExit(f"Unknown tool: {args.tool}")


def main() -> int:
    ap = argparse.ArgumentParser(description="jev-engine MCP stdio diagnostic")
    ap.add_argument("--tool", required=True)
    ap.add_argument("--task")
    ap.add_argument("--command")
    ap.add_argument("--mcps", help='JSON list of MCP servers for --tool select_mcp_tools, or @file to read it')
    ap.add_argument("--root_dir", default=".")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    # A bare name (`python`, `python3`) is resolved through PATH by the OS at
    # spawn time, so an existence check on it would be wrong, not merely strict.
    if not os.path.dirname(PYTHON) or os.path.exists(PYTHON):
        pass
    else:
        print(
            f"Python not found: {PYTHON}\n"
            "Set JEV_DIAG_PYTHON to the interpreter that has requirements.txt installed, "
            "or create the repo venv first.",
            file=sys.stderr,
        )
        return 2
    if not os.path.exists(SERVER):
        print(f"Server not found: {SERVER}", file=sys.stderr)
        return 2
    if args.tool == "guardrail_command" and not args.command:
        ap.error("--command is required for guardrail_command")
    if args.tool != "guardrail_command" and not args.task:
        ap.error(f"--task is required for {args.tool}")

    env = dict(os.environ)
    if args.mock:
        env["JEV_MCP_MOCK"] = "1"
    cwd = args.cwd or Path(SERVER).parent

    proc = subprocess.Popen(
        [PYTHON, SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        encoding="utf-8",
    )

    failed = False
    try:
        init = rpc(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "diag_mcp", "version": "0"},
            },
        })
        print("initialize:", "ok" if init and "result" in init else json.dumps(init)[:200])

        tools = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = sorted(t["name"] for t in tools.get("result", {}).get("tools", []))
        print("tools:", names)

        call_args = build_args(args)
        print(f"tools/call {args.tool} args={json.dumps(call_args)}")
        call = rpc(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": args.tool, "arguments": call_args},
        })

        if call is None:
            print("NO RESPONSE from server")
            failed = True
        else:
            raw = json.dumps(call)
            try:
                json.loads(raw)
                parse_ok = True
            except Exception as e:
                parse_ok = False
                print("NOT strict-JSON:", e)
            print(f"strict-JSON: {'YES' if parse_ok else 'NO'}")
            result = call.get("result", {})
            is_error = result.get("isError", False)
            print(f"isError: {str(is_error).lower()}")
            if is_error:
                failed = True
            content = result.get("content", [])
            text = content[0].get("text", "") if content else ""
            print(f"response size: {len(raw)} bytes, text {len(text)} chars")
            json_text = text
            if text.startswith("Error executing tool ") and ": {" in text:
                json_text = text[text.index(": {") + 2:]
            try:
                body = json.loads(json_text)
                if "error" in body and isinstance(body["error"], dict) and "code" in body["error"]:
                    print("ERROR ENVELOPE:", json.dumps(body["error"]))
                    failed = True
                else:
                    print("result keys:", list(body.keys()))
            except json.JSONDecodeError:
                print("tool text is not JSON:", text[:200])
                failed = True
    except Exception as e:
        print("DIAG EXC:", type(e).__name__, e)
        failed = True
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read()
        if err.strip():
            print("server stderr tail:", err.strip()[-300:])

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())