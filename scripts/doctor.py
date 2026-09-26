"""Check the jev-engine installation: interpreter, deps, config, settings, plugin drift.

Read-only. Nothing here starts a child process, calls the TypeSafe API, or
writes a file. Exits 0 when healthy, 1 when something needs attention.

    & .\\.venv\\Scripts\\python.exe scripts\\doctor.py

Every failure prints a one-line fix, because the alternative is the ad-hoc
troubleshooting section that used to live in `config/README.md`.
"""

import argparse
import hashlib
import json
import os
import platform
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OK = "ok"
WARN = "warn"
BAD = "bad"

_FAILURES = 0
_WARNINGS = 0


def report(status, check, detail="", fix=""):
    global _FAILURES, _WARNINGS
    if status == BAD:
        _FAILURES += 1
    elif status == WARN:
        _WARNINGS += 1
    mark = {OK: "[ok]", WARN: "[!!]", BAD: "[XX]"}[status]
    print(f"  {mark} {check}")
    if detail:
        print(f"        {detail}")
    if fix and status != OK:
        print(f"        fix: {fix}")
    return status


def check_interpreter():
    print("interpreter")
    version = platform.python_version()
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    report(
        OK if in_venv else WARN,
        f"python {version} at {sys.executable}",
        "" if in_venv else "not running from a virtual environment",
        fix="use the repo venv: .\\.venv\\Scripts\\python.exe scripts\\doctor.py",
    )
    if platform.system() == "Windows" and not in_venv:
        report(WARN, "32-bit interpreter on Windows", "the SDK wheels are 64-bit")


def check_sdk():
    print("dependencies")
    try:
        import typesafe_sdk

        version = getattr(typesafe_sdk, "__version__", "unknown")
        client = getattr(typesafe_sdk, "TypeSafeClient", None)
        if client is None:
            report(BAD, "typesafe_sdk imports", f"version {version}, but no TypeSafeClient",
                   fix="& .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
            return
        report(OK, "typesafe_sdk importable", f"version {version}")
    except Exception as e:
        report(BAD, "typesafe_sdk importable", f"{type(e).__name__}: {e}",
               fix="& .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return

    try:
        from mcp.server.mcpserver import MCPServer
    except Exception as e:
        report(BAD, "mcp.MCPServer importable", f"{type(e).__name__}: {e}",
               fix="the pinned mcp package must expose mcp.server.mcpserver.MCPServer")
        return
    report(OK, "mcp.MCPServer importable", MCPServer.__module__)

    # The server module imports every tool body; if it loads, the wiring is sound.
    # Importing it emits `server_start`, so quiet the logger *first* — and do it
    # through `get_logger()`, because that call is what (re)sets the level to
    # INFO. A health check that prints log lines of its own is not a readable
    # health check.
    import logging

    import jev_logging

    jev_logging.get_logger().setLevel(logging.WARNING)
    try:
        import jev_mcp

        tools = sorted(
            name
            for name in dir(jev_mcp)
            if name in {"guardrail_command", "search_agent_skills", "search_target_files", "select_model_tier"}
        )
        expected = {"guardrail_command", "search_agent_skills", "search_target_files", "select_model_tier"}
        if set(tools) == expected:
            report(OK, "jev_mcp imports", f"{len(tools)} tools registered")
        else:
            report(BAD, "jev_mcp imports", f"registered {tools}", fix="expected all four tools")
    except Exception as e:
        report(BAD, "jev_mcp imports", f"{type(e).__name__}: {e}",
               fix="run: & .\\.venv\\Scripts\\python.exe -c \"import jev_mcp\"")


def check_api_key():
    print("credentials")
    from config import ensure_dotenv

    ensure_dotenv()
    key = (os.getenv("TYPESAFE_API_KEY") or "").strip()
    if not key:
        report(BAD, "TYPESAFE_API_KEY", "not set",
               fix="set it in the opencode MCP `environment` block, or in the repo .env (gitignored)")
        return
    # Length and a 4-character suffix only. The value never reaches stdout, the
    # log, or this process's argv.
    from jev_logging import _redact

    shown = _redact(key[-4:])
    report(OK, "TYPESAFE_API_KEY", f"present, {len(key)} chars, ends ...{shown}")
    if os.getenv("JEV_MCP_MOCK") in {"1", "true", "yes", "on"}:
        report(WARN, "JEV_MCP_MOCK", "mock judge is active; every decision is the offline stub",
               fix="set JEV_MCP_MOCK=0 for live decisions")


def check_settings():
    print("settings")
    import jev_engine
    from jev_engine import _reset_settings_cache, get_scan_paths, load_jev_settings

    _reset_settings_cache()
    settings = load_jev_settings()
    sources = settings.get("sources") or []
    if sources:
        report(OK, "jevs_settings.json resolved", f"{len(sources)} file(s) merged")
        for src in sources:
            print(f"        - {src}")
    else:
        report(WARN, "jevs_settings.json resolved", "no settings file found; defaults in use",
               fix="copy config\\opencode.example.json's jevs_settings block, or create jevs_settings.json")

    try:
        paths = get_scan_paths(REPO)
    except Exception as e:
        report(BAD, "scan paths resolve", f"{type(e).__name__}: {e}")
        paths = []
    existing = [p for p in paths if p.is_dir()]
    report(
        OK if existing else WARN,
        "scan paths",
        f"{len(existing)}/{len(paths)} exist under {REPO}",
        fix="" if existing else "these are searched for skills/workflows: " + ", ".join(p.name for p in paths[:4]),
    )


def check_root_allowlist():
    print("root_dir allowlist")
    import jev_engine
    from config import get_config

    cfg = get_config()
    configured = list(cfg.allowed_roots)
    if configured:
        report(OK, "JEV_MCP_ALLOWED_ROOTS", f"{len(configured)} entr(y/ies): " + ", ".join(configured))
    else:
        report(OK, "JEV_MCP_ALLOWED_ROOTS", "unset; the CWD and its ancestors are the allowlist")

    try:
        resolved = jev_engine._validate_root_dir(".")
        report(OK, "cwd is inside the allowlist", str(resolved))
    except Exception as e:
        report(BAD, "cwd is inside the allowlist", f"{type(e).__name__}: {e}",
               fix="start the server with cwd set to the project, or add the path to JEV_MCP_ALLOWED_ROOTS")


def check_log_dir():
    print("logging")
    from jev_logging import log_path

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as e:
        report(BAD, "log directory writable", f"{path.parent}: {type(e).__name__}: {e}",
               fix="point JEV_MCP_LOG_FILE at a writable directory")
        return
    report(OK, "log directory writable", str(path))


def check_thresholds():
    print("budgets")
    # Read the environment directly rather than through `get_config()`: a bad
    # value there raises, and the whole point of this step is to say *which*
    # value is bad and what it should be.
    from config import (
        DEFAULT_AUTH_COOLDOWN_S,
        DEFAULT_BREAKER_COOLDOWN_S,
        DEFAULT_BREAKER_THRESHOLD,
        DEFAULT_TIMEOUT_MS,
    )
    from policy import DEFAULT_AUTO_ACCEPT, DEFAULT_REVIEW_AT

    def _number(name, fallback, cast=float):
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return fallback, None
        try:
            return cast(raw), None
        except ValueError:
            return None, f"{name}={raw!r} is not a number"

    timeout, bad = _number("JEV_MCP_TIMEOUT_MS", DEFAULT_TIMEOUT_MS, int)
    if bad:
        report(BAD, "JEV_MCP_TIMEOUT_MS", bad, fix=f"an integer of milliseconds, e.g. {DEFAULT_TIMEOUT_MS}")
    else:
        report(OK, "JEV_MCP_TIMEOUT_MS", f"{timeout} ms (total per tool call, retries included)")

    auto_accept, bad_a = _number("JEV_MCP_AUTO_ACCEPT", DEFAULT_AUTO_ACCEPT)
    review_at, bad_r = _number("JEV_MCP_REVIEW_AT", DEFAULT_REVIEW_AT)
    for name, value, bad_value in (
        ("JEV_MCP_AUTO_ACCEPT", auto_accept, bad_a),
        ("JEV_MCP_REVIEW_AT", review_at, bad_r),
    ):
        if bad_value:
            report(BAD, name, bad_value, fix="a number in [0, 1]")
        elif not 0 <= value <= 1:
            report(BAD, name, f"{value} is outside [0, 1]", fix="a number in [0, 1]")

    if not (bad_a or bad_r) and auto_accept is not None and review_at is not None:
        if review_at > auto_accept:
            report(BAD, "review_at <= auto_accept",
                   f"JEV_MCP_REVIEW_AT={review_at} exceeds JEV_MCP_AUTO_ACCEPT={auto_accept}",
                   fix="lower JEV_MCP_REVIEW_AT, or raise JEV_MCP_AUTO_ACCEPT (both in [0, 1])")
        else:
            report(OK, "review_at <= auto_accept", f"{review_at} <= {auto_accept}")

    threshold, _ = _number("JEV_MCP_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD, int)
    cooldown, _ = _number("JEV_MCP_BREAKER_COOLDOWN_S", DEFAULT_BREAKER_COOLDOWN_S)
    auth_cooldown, _ = _number("JEV_MCP_AUTH_COOLDOWN_S", DEFAULT_AUTH_COOLDOWN_S)
    report(
        OK,
        "circuit breaker",
        f"opens after {threshold} failures, "
        f"{cooldown}s cooldown, {auth_cooldown}s for auth errors",
    )


def check_plugin_drift():
    print("opencode plugin")
    example = REPO / "config" / "jev-plugin.example.js"
    installed = Path.home() / ".config" / "opencode" / "plugins" / "jev-plugin.js"

    if not example.is_file():
        report(BAD, "config/jev-plugin.example.js exists", str(example))
        return
    if not installed.is_file():
        report(WARN, "plugin installed", f"not found at {installed}",
               fix="Copy-Item config\\jev-plugin.example.js \"$env:USERPROFILE\\.config\\opencode\\plugins\\jev-plugin.js\"")
        return

    def _sha256(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    example_hash, installed_hash = _sha256(example), _sha256(installed)
    if example_hash == installed_hash:
        report(OK, "installed plugin matches the example", f"sha256 {example_hash[:12]}")
    else:
        report(BAD, "installed plugin matches the example",
               f"installed sha256 {installed_hash[:12]} != example {example_hash[:12]}",
               fix="Copy-Item config\\jev-plugin.example.js \"$env:USERPROFILE\\.config\\opencode\\plugins\\jev-plugin.js\" -Force")
    if example.stat().st_mtime > installed.stat().st_mtime:
        report(WARN, "the example is newer than the installed plugin", "a copy is probably outstanding")


def main():
    ap = argparse.ArgumentParser(description="Read-only health check for a jev-engine installation.")
    ap.add_argument("--json", action="store_true", help="Emit the checklist as JSON instead of text.")
    args = ap.parse_args()

    import io
    from contextlib import redirect_stdout

    steps = (
        check_interpreter,
        check_sdk,
        check_api_key,
        check_settings,
        check_root_allowlist,
        check_log_dir,
        check_thresholds,
        check_plugin_drift,
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        for step in steps:
            # A broken environment is the *most likely* reason to run the doctor,
            # so a step that raises on a bad env var is a finding, not a crash.
            try:
                step()
            except Exception as e:
                report(
                    BAD,
                    step.__name__.replace("check_", "").replace("_", " "),
                    f"{type(e).__name__}: {e}",
                    fix="check the JEV_MCP_* / TYPESAFE_* environment variables against .env.example",
                )
        print()
        if _FAILURES:
            verdict = f"{_FAILURES} problem(s) need attention" + (f", {_WARNINGS} warning(s)" if _WARNINGS else "")
        elif _WARNINGS:
            verdict = f"healthy, with {_WARNINGS} warning(s)"
        else:
            verdict = "healthy"

    if args.json:
        print(json.dumps({"healthy": _FAILURES == 0, "failures": _FAILURES,
                          "warnings": _WARNINGS, "report": buffer.getvalue()}, indent=2))
    else:
        print(f"jev-engine doctor - {REPO}")
        print(buffer.getvalue().rstrip())
        print()
        print(f"  {verdict}")
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
