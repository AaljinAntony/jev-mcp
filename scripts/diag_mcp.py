"""Transport-level MCP diagnostic client for jev-engine.

Spawns ``jev_mcp.py`` over stdio (the same way opencode does) and runs one tool
call, printing the raw JSON-RPC response plus the server's stderr. Run it from
any workspace to reproduce a tool failure with a specific ``root_dir`` and task.

Examples::

    python scripts/diag_mcp.py --tool guardrail_command --command "git status"
    python scripts/diag_mcp.py --tool search_agent_skills --task "find skills for ui bug fix" --root_dir D:\\Godot_projects\\flux-wall
    python scripts/diag_mcp.py --tool search_target_files --task "find config" --root_dir D:\\Godot_projects\\flux-wall --mock

Relevant env overrides: ``JEV_DIAG_PYTHON``, ``JEV_DIAG_SERVER``.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = os.environ.get("JEV_DIAG_PYTHON") or str(REPO / ".venv" / "Scripts" / "python.exe")
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
    if args.tool in ("search_agent_skills", "search_target_files"):
        return {"task": args.task, "root_dir": args.root_dir}
    raise SystemExit(f"Unknown tool: {args.tool}")


def main() -> int:
    ap = argparse.ArgumentParser(description="jev-engine MCP stdio diagnostic")
    ap.add_argument("--tool", required=True)
    ap.add_argument("--task")
    ap.add_argument("--command")
    ap.add_argument("--root_dir", default=".")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(PYTHON):
        print(f"Python not found: {PYTHON}", file=sys.stderr)
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
            content = result.get("content", [])
            text = content[0].get("text", "") if content else ""
            print(f"response size: {len(raw)} bytes, text {len(text)} chars")
            try:
                body = json.loads(text)
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