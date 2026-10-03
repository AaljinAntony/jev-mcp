"""Check the jev-engine installation: interpreter, deps, config, settings, plugin drift.

Read-only. Nothing here calls the TypeSafe API or writes a file. The one child
process is `opencode --version`, and only so the MCP config can be checked
against the schema the installed host actually uses; it is skipped when opencode
is not on PATH. Exits 0 when healthy, 1 when something needs attention.

    & .\\.venv\\Scripts\\python.exe scripts\\doctor.py

Every failure prints a one-line fix, because the alternative is the ad-hoc
troubleshooting section that used to live in `config/README.md`.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
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

_WINDOWS = platform.system() == "Windows"

# A health check that prints a command the user cannot run is worse than one that
# prints nothing, so every fix string is built for the host platform. The
# venv layout differs too: `.venv\Scripts\python.exe` on Windows,
# `.venv/bin/python` everywhere else.
_VENV_PY = ".\\.venv\\Scripts\\python.exe" if _WINDOWS else ".venv/bin/python"
_PIP = f"{_VENV_PY} -m pip install -r requirements.txt"
_PLUGIN_DEST = (
    r"%USERPROFILE%\.config\opencode\plugins\jev-plugin.js"
    if _WINDOWS
    else "~/.config/opencode/plugins/jev-plugin.js"
)


def _copy_hint(src: str, *, force: bool = False) -> str:
    """The command that installs a config example where the host expects it."""
    if _WINDOWS:
        suffix = " -Force" if force else ""
        return f'Copy-Item {src} "{_PLUGIN_DEST}"{suffix}'
    return f"cp {src} {_PLUGIN_DEST}"


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
        fix=f"use the repo venv: {_VENV_PY} scripts/doctor.py",
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
                   fix=_PIP)
            return
        report(OK, "typesafe_sdk importable", f"version {version}")
    except Exception as e:
        report(BAD, "typesafe_sdk importable", f"{type(e).__name__}: {e}",
               fix=_PIP)
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

        expected = {
            "guardrail_command",
            "search_agent_skills",
            "search_target_files",
            "select_mcp_tools",
            "select_model_tier",
        }
        tools = sorted(name for name in dir(jev_mcp) if name in expected)
        if set(tools) == expected:
            report(OK, "jev_mcp imports", f"{len(tools)} tools registered")
        else:
            report(BAD, "jev_mcp imports", f"registered {tools}", fix="expected all five tools")
    except Exception as e:
        report(BAD, "jev_mcp imports", f"{type(e).__name__}: {e}",
               fix=f'run: {_VENV_PY} -c "import jev_mcp"')


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
               fix="copy config\\jevs_settings.example.json to jevs_settings.json "
                   "(never into opencode.json - its schema rejects the block)")

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


def check_global_scan_paths():
    """Report the per-user skill directories that are searched on every call."""
    print("global skill scan")
    from jev_engine import GLOBAL_SCAN_RELS, _global_scan_dirs

    try:
        dirs = _global_scan_dirs()
    except Exception as e:
        report(BAD, "global scan dirs resolve", f"{type(e).__name__}: {e}")
        return
    if dirs:
        report(OK, "global skill directories", f"{len(dirs)} in use")
        for d in dirs:
            print(f"        - {d}")
    else:
        report(
            WARN,
            "global skill directories",
            "none exist; only the workspace is searched",
            fix="install skills under one of: " + ", ".join(GLOBAL_SCAN_RELS),
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


# ----------------------------------------------------------------------
# OpenCode MCP config
#
# OpenCode has two incompatible MCP config shapes and puts no version marker in
# the file, so the shape is read from the config itself and cross-checked against
# the installed binary when it is on PATH:
#
#   V1 (opencode 1.x)   mcp.<name>          toggled with `enabled`
#   V2 (opencode 2.x)   mcp.servers.<name>  toggled with `disabled`
#
# Both accept `type`, `command`, `cwd`, `environment` and `timeout`. V2 adds
# `codemode` (default true), `protocol` and `mcp.timeout.*`.
#
# Sources: opencode.ai/docs/en/mcp-servers and opencode.ai/v2/docs/mcp-servers.
# The V2 page states outright that server names do not sit directly under `mcp`
# and that there is no `enabled` field. A config that does not match the running
# version is rejected rather than ignored, so the server disappears from the list
# instead of reporting an error - the hardest failure mode to diagnose from the
# symptom, and the reason this step exists at all.
# ----------------------------------------------------------------------
MCP_SERVER_NAME = "jev-engine"

#: Tokens meaning a path was never substituted. `config/opencode.example.json`
#: ships `<REPO_DIR>` placeholders and the documented install step copies the file
#: verbatim, so spawning a path that does not exist is the most likely way to end
#: up with a server that is simply absent. Deliberately narrow - angle-bracket
#: forms only, since a loose token would fire on a real directory name and turn a
#: working config into a false alarm.
_PLACEHOLDER_TOKENS = ("<repo_dir>", "<repo", "<your", "<path")


def _mcp_config_files():
    """The opencode config files the host may load, highest precedence first.

    Same order the plugin's `resolveServerCommand` uses (project, then
    `.opencode/`, then the user-level file), so a config this step calls valid is
    the config the plugin will actually spawn from.
    """
    cwd = Path.cwd()
    candidates = [
        cwd / "opencode.json",
        cwd / ".opencode" / "opencode.json",
        Path.home() / ".config" / "opencode" / "opencode.json",
    ]
    return [p for p in candidates if p.is_file()]


def _server_entry(config, name):
    """`(entry, shape)` for `name` in `config`, or `(None, None)`.

    Both shapes are read and the shape returned rather than assumed, because the
    legal enable/disable key differs between them.
    """
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        return None, None
    direct = mcp.get(name)
    if isinstance(direct, dict):
        return direct, "v1"
    nested = mcp.get("servers")
    if isinstance(nested, dict) and isinstance(nested.get(name), dict):
        return nested[name], "v2"
    return None, None


def _opencode_major():
    """The installed opencode major version, or None when undeterminable.

    Best effort and never fatal. opencode may simply not be on PATH - the server
    runs fine from Claude Code, Cursor or any other MCP host - and a version this
    cannot read must not turn into a health-check failure.
    """
    exe = shutil.which("opencode")
    if not exe:
        return None
    try:
        done = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.", (done.stdout or "") + (done.stderr or ""))
    return int(match.group(1)) if match else None


def _reshape_hint(expected):
    """The one-line fix for a config written in the other version's shape."""
    if expected == "v2":
        return 'nest it under "mcp": { "servers": { "jev-engine": { ... } } }, and use "disabled"'
    return 'flatten it to "mcp": { "jev-engine": { ... } }, and use "enabled"'


def check_mcp_config():
    print("opencode mcp config")
    major = _opencode_major()
    if major is None:
        report(WARN, "opencode version", "not readable on PATH; shape checked on its own",
               fix="install opencode, or ignore this if the host is not opencode")
        expected = None
    else:
        expected = "v1" if major < 2 else "v2"
        report(OK, "opencode version", f"{major}.x (expects the {expected.upper()} shape)")

    files = _mcp_config_files()
    if not files:
        report(WARN, "opencode.json found", "no project or user-level config",
               fix="see config/README.md; the server also runs from any other MCP host")
        return

    found = None
    for path in files:
        try:
            # utf-8-sig, not utf-8: Windows PowerShell's `Set-Content -Encoding utf8`
            # and the redirect operator both write a BOM, and a BOM makes json.loads
            # raise. It is byte-identical to utf-8 when there is no BOM, so a
            # hand-written file reads the same either way.
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            report(BAD, f"{path.name} parses", f"{type(e).__name__}: {e}",
                   fix="a malformed file invalidates itself and every server in it")
            continue
        if not isinstance(raw, dict):
            continue
        entry, shape = _server_entry(raw, MCP_SERVER_NAME)
        if entry is not None:
            found = (path, entry, shape)
            break

    if found is None:
        report(BAD, f"'{MCP_SERVER_NAME}' configured",
               f"not present in any of: " + ", ".join(str(p) for p in files),
               fix="add the mcp block from config/opencode.example.json to your user-level opencode.json")
        return

    path, entry, shape = found
    report(OK, f"'{MCP_SERVER_NAME}' configured", f"{path} ({shape.upper()} shape)")

    if expected is not None and shape != expected:
        report(BAD, "config shape matches the installed opencode",
               f"config is {shape.upper()}, opencode {major}.x expects {expected.upper()}",
               fix=_reshape_hint(expected))

    command = entry.get("command")
    if isinstance(command, str):
        # The host accepts a bare string; the plugin's resolver wants an array.
        report(WARN, "command shape", "a bare string, not an array",
               fix='use ["<python path>", "<path to jev_mcp.py>"]')
        argv = [command]
    elif isinstance(command, list) and command and all(isinstance(a, str) for a in command):
        argv = list(command)
    else:
        report(BAD, "command", f"{type(command).__name__}; expected a non-empty array of strings",
               fix='["<python path>", "<path to jev_mcp.py>"]')
        return

    unsubstituted = [p for p in argv if any(t in p.lower() for t in _PLACEHOLDER_TOKENS)]
    if unsubstituted:
        report(BAD, "command paths substituted", "placeholder left in: " + ", ".join(unsubstituted),
               fix="replace <REPO_DIR> with absolute paths; copying the example verbatim does not")
    missing = [p for p in argv if not Path(p).exists()]
    if missing:
        report(BAD, "command paths exist", "missing: " + ", ".join(missing),
               fix="point command at this checkout's venv interpreter and jev_mcp.py")
    if not unsubstituted and not missing:
        report(OK, "command paths exist", f"{len(argv)} path(s) resolved")

    kind = entry.get("type")
    if kind is None:
        report(WARN, "type", "absent; the host assumes local for a command entry", fix='"type": "local"')
    elif kind != "local":
        report(BAD, "type", f"{kind!r}; this server is spawned over stdio", fix='"type": "local"')
    else:
        report(OK, "type", "local")

    # V1 toggles with `enabled`, V2 with `disabled`. The other key is not in the
    # schema, and a schema violation is what takes the whole file down.
    if shape == "v2" and "enabled" in entry:
        report(BAD, "enable key", "'enabled' is not a V2 field",
               fix='rename it to "disabled"')
    elif shape == "v1" and "disabled" in entry:
        report(BAD, "enable key", "'disabled' is not a V1 field", fix='rename it to "enabled"')
    elif "enabled" in entry or "disabled" in entry:
        report(OK, "enable key", "enabled" if "enabled" in entry else "disabled")
    else:
        report(OK, "enable key", "absent; the server connects by default")

    env = entry.get("environment")
    if isinstance(env, dict) and env:
        report(OK, "environment block", f"{len(env)} variable(s): " + ", ".join(sorted(env)[:6]))
    else:
        report(OK, "environment block",
               "absent; every JEV_MCP_* knob falls back to its documented default")

    if shape == "v2":
        codemode = entry.get("codemode")
        if codemode is None:
            report(WARN, "codemode",
                   "V2 defaults to true, which routes this server's tools through Code Mode "
                   "instead of putting them on the model's native tool list",
                   fix='"codemode": false - the agent is told to call these five tools by name')
        elif codemode is False:
            report(OK, "codemode", "false; tools are on the model's native tool list")
        else:
            report(OK, "codemode", str(codemode))


def check_plugin_drift():
    print("opencode plugin")
    example = REPO / "config" / "jev-plugin.example.js"
    installed = Path.home() / ".config" / "opencode" / "plugins" / "jev-plugin.js"

    if not example.is_file():
        report(BAD, "config/jev-plugin.example.js exists", str(example))
        return
    if not installed.is_file():
        report(WARN, "plugin installed", f"not found at {installed}",
               fix=_copy_hint("config/jev-plugin.example.js"))
        return

    def _sha256(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    example_hash, installed_hash = _sha256(example), _sha256(installed)
    if example_hash == installed_hash:
        report(OK, "installed plugin matches the example", f"sha256 {example_hash[:12]}")
    else:
        report(BAD, "installed plugin matches the example",
               f"installed sha256 {installed_hash[:12]} != example {example_hash[:12]}",
               fix=_copy_hint("config/jev-plugin.example.js", force=True))
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
        check_global_scan_paths,
        check_root_allowlist,
        check_log_dir,
        check_thresholds,
        check_mcp_config,
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
