# jev-engine — Jev AI (TypeSafe AI) MCP Server

A Model Context Protocol (MCP) server registered as **`jev-engine`** over stdio. It wraps the Jev AI (TypeSafe AI) deterministic decision engine so any MCP-capable model (Claude, GPT, DeepSeek, local models) can run guardrail checks, skill routing, file targeting, and model-tier selection through four JSON-RPC tools.

---

## System at a Glance

| Item | Value |
|---|---|
| **Server identity** | `jev-engine` (MCP over stdio) |
| **Hosting directory** | `D:\mcp\jev-typesafe-mcp` |
| **Interpreter** | `.venv\Scripts\python.exe` (isolated virtual environment) |
| **Decision engine** | `TypeSafeClient.system_one(state=..., questions=...)` |
| **Required SDK** | `typesafe-sdk` (`TypeSafeClient`, `Choice`, `Noul`, `Score`) — **never** `JevClient` or `typesafe_ai` |
| **MCP SDK** | `mcp==2.2.0` — uses `MCPServer` (`from mcp.server.mcpserver import MCPServer as FastMCP`) |
| **Model compatibility** | Model-agnostic; any MCP client with stdio tool support |

> **Compatibility note:** The `mcp.server.fastmcp` module was **removed** in mcp 2.x (FastMCP renamed to `MCPServer`). `jev_mcp.py` therefore imports `MCPServer as FastMCP` directly. Do **not** pin `mcp<2` unless you intentionally move to the v1 line.

---

## File & Config Locations

| Path | Purpose |
|---|---|
| `D:\mcp\jev-typesafe-mcp\jev_engine.py` | Core decision logic, config loader, CLI entry point |
| `D:\mcp\jev-typesafe-mcp\jev_mcp.py` | MCP server: registers the 4 tools, delegates to `jev_engine` |
| `D:\mcp\jev-typesafe-mcp\requirements.txt` | Pinned Python dependencies (UTF-8) |
| `D:\mcp\jev-typesafe-mcp\.env` | Local secrets — holds `TYPESAFE_API_KEY` (never committed) |
| `D:\mcp\jev-typesafe-mcp\.env.example` | Template showing the required key format |
| `D:\mcp\jev-typesafe-mcp\.venv\` | Isolated virtual environment (Python 3.14) |
| `D:\mcp\jev-typesafe-mcp\README.md` | This document |
| `D:\mcp\jev-typesafe-mcp\config\opencode.example.json` | Sanitized OpenCode config template (safe to commit — no secrets) |
| `D:\mcp\jev-typesafe-mcp\config\jev-plugin.example.js` | Sanitized plugin template (safe to commit) |
| `D:\mcp\jev-typesafe-mcp\config\README.md` | Install guide for deploying the examples |
| `C:\Users\aalji\.config\opencode\opencode.json` | OpenCode MCP server registration + `jev_settings` |
| `C:\Users\aalji\.config\opencode\plugins\jev-plugin.js` | Optional OpenCode plugin (hook-based Jev routing) |

---

## Architecture

```
OpenCode (or any MCP client)
   │  stdio JSON-RPC
   ▼
jev_mcp.py  ── MCPServer("jev-engine") ── 4 tools
   │  delegates
   ▼
jev_engine.py  ── TypeSafeClient.system_one(state, questions)
   │  reads
   ▼
opencode.json → jev_settings  (enable_model_routing, models, scan_paths)
.env → TYPESAFE_API_KEY
```

### Decision flow

1. A tool receives a prompt/task/command via MCP.
2. `jev_engine` calls `TypeSafeClient.system_one(state=..., questions=...)` using `Noul` / `Choice` primitives.
3. Answers are extracted with `get_answer()` / `get_val()` / `get_prob()` (safe against both SDK object and JSON-dict answer shapes).
4. A JSON-serializable dict payload is returned to the caller.

---

## Environment & Setup

```powershell
cd D:\mcp\jev-typesafe-mcp

# 1. Create/activate the venv (already committed to disk here)
.\.venv\Scripts\python.exe --version

# 2. Install pinned dependencies
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Configure the API key (copy the template, fill in the real key)
Copy-Item .env.example .env
```

### `.env` / `.env.example`

```dotenv
TYPESAFE_API_KEY=apikey_********************************
```

Both `jev_engine.py` and `jev_mcp.py` load `.env` via `python-dotenv` with `override=True` (relative to the file's parent directory). Because the MCP config already injects `TYPESAFE_API_KEY`, the `.env` file acts as a reliable fallback.

### Verify the SDK is healthy

```powershell
& .\.venv\Scripts\python.exe -c "import typesafe_sdk; print(('TypeSafeClient','Choice','Noul','Score') , [n for n in ('TypeSafeClient','Choice','Noul','Score') if n in dir(typesafe_sdk)]); print('JevClient present?', 'JevClient' in dir(typesafe_sdk))"
```

---

## Core MCP Tools

Registered by `jev_mcp.py`; all return JSON-serializable dictionaries.

### 1. `guardrail_command` — terminal command safety

Pre-execution audit for shell commands.

- **Params:** `command` (string, required)
- **Jev primitives:** `is_destructive` (`Noul`), `modifies_git` (`Noul`)
- **Thresholds:** safe only if `destructive_prob < 0.20` **and** `git_modify_prob < 0.20`

```json
{ "safe": true, "destructive_prob": 0.01, "git_modify_prob": 0.01 }
```

### 2. `search_agent_skills` — dynamic context pruning & skill routing

Scans workspace agent docs and returns only relevant Markdown, inlining up to 6,000 chars per resource.

- **Params:** `task` (required), `root_dir` (default `.`)
- **Scan dirs:** `.agents/skills`, `.agents/workflows`, `.agents/memory`, `.opencode/skills`, `skills`, `.agents` **plus** any extras from `jev_settings.scan_paths`
- **Jev primitives:** `primary` / `secondary` / `tertiary` (`Choice`, `criteria={path: description}`)
- **Key capabilities:** probability matching (secondary picks ≥ 0.12), sibling-prefix clustering (e.g. `godot-ui-*`)

```json
{
  "matched": true,
  "count": 2,
  "primary": { "name": "godot-ui-theme", "file": ".agents/skills/godot-ui-theme/SKILL.md", "content": "..." },
  "resources": [...],
  "summary": "Found 2 relevant agent resource(s): godot-ui-theme, godot-ui-layout"
}
```

### 3. `search_target_files` — fast workspace file selector

Filters the repo tree down to task-relevant files.

- **Params:** `task` (required), `root_dir` (default `.`)
- **Exclusions:** `.git`, `.godot`, `.import`, `.venv`, `node_modules`, `dist`, `build` + media/binary extensions (`.png`, `.jpg`, `.wav`, `.mp3`, `.zip`, ...)
- **Jev primitive:** `target_file` (`Choice`, `criteria={path: description}`)

```json
{ "matched": true, "files": ["src/core/player_controller.gd"] }
```

### 4. `select_model_tier` — dynamic model tier routing

Analyzes task complexity and assigns a tier. **Gated by `jev_settings.enable_model_routing`** (default `false`).

- **When off:** returns immediately, disabled payload, **no Jev API call**, no model switching.
- **When on:** runs a `Choice` over `fast` / `balanced` / `frontier`, then resolves the configured model for the chosen tier.

```json
{
  "enabled": true,
  "task": "Refactor auth middleware to support OAuth2 refresh tokens",
  "recommended_tier": "frontier",
  "recommended_model": "<frontier model id>",
  "model_map": { "fast": "...", "balanced": "...", "frontier": "..." }
}
```

```json
{ "enabled": false, "task": "...", "recommended_tier": null, "recommended_model": null, "model_map": {} }
```

---

## Configuration — `jev_settings` in opencode.json

File: `C:\Users\aalji\.config\opencode\opencode.json` (user-level). Project-level `opencode.json` / `.opencode/opencode.json` take priority when present. The MCP server reads these files itself at call time via `load_jev_settings()`.

```jsonc
{
  "mcp": {
    "jev-engine": {
      "type": "local",
      "enabled": true,
      "command": "D:/mcp/jev-typesafe-mcp/.venv/Scripts/python.exe",
      "args": ["D:/mcp/jev-typesafe-mcp/jev_mcp.py"],
      "env": { "TYPESAFE_API_KEY": "${TYPESAFE_API_KEY}" }
    }
  },
  "jev_settings": {
    "enable_model_routing": false,
    "models": {
      "fast":     "",      // <-- fill with model IDs when routing is enabled
      "balanced": "",
      "frontier": ""
    },
    "scan_paths": []        // <-- extra skill dirs, appended to the defaults
  }
}
```

### Semantics

| Key | Type | Meaning |
|---|---|---|
| `enable_model_routing` | bool | Master switch for `select_model_tier`. Off = no Jev call / no model switching. |
| `models` | `{fast, balanced, frontier}` | Tier → model ID mapping returned via `recommended_model` + `model_map`. Empty entries are ignored. |
| `scan_paths` | `[relative path]` | **Additive** extras to the default skill dirs. Deduplicated on load. |

The first config file that contains a `jev_settings` block wins (project > user). Malformed JSON never breaks the server — it logs to stderr and falls back to defaults.

---

## The OpenCode Plugin — `jev-plugin.js`

Location: `C:\Users\aalji\.config\opencode\plugins\jev-plugin.js`

An auxiliary OpenCode hook (`chat.message`) that, when it finds Markdown skill/workflow/memory files in the workspace, uses Jev to pick the single most relevant file and **injects its content** directly into the conversation context (`[Active Capability / Skill: <path>]`).

### How it works

1. Discovers config via the same candidate paths as the MCP server:
   - `./opencode.json`, `./.opencode/opencode.json`, `~/.config/opencode/opencode.json`
2. Extracts the Python interpreter from `mcp["jev-engine"].command` (falls back to `D:\mcp\jev-typesafe-mcp\.venv\Scripts\python.exe`).
3. Loads `TYPESAFE_API_KEY` from the process env, else from `D:\mcp\jev-typesafe-mcp\.env`.
4. Uses `jev_settings.scan_paths` (default `[".agents/skills", ".agents/workflows", ".agents/memory", ".opencode/skills"]`).
5. Recursively scans the resolved dirs for `*.md`, spawns Python via `spawnSync` to query Jev, and injects the winning file's content.

### ⚠️ Known limitation

The plugin's inline Python currently targets the **legacy** `JevClient` / `Choice(options=...)` / `client.decide(...)` API, which **does not exist in `typesafe-sdk==0.7.1`**. The plugin fails gracefully (never crashes OpenCode — "Execution bypassed safely"), but the hook currently performs no routing.

If you want the plugin to actually route, the embedded script must be migrated to:

```python
from typesafe_sdk import TypeSafeClient, Choice

client = TypeSafeClient(api_key=os.getenv("TYPESAFE_API_KEY"))
res = client.system_one(
    state="User Task: ...",
    questions={"target": Choice(criteria={path: "Agent resource" for path in ...})},
)
# selected = res.answers["target"].choice
```

The MCP-based `search_agent_skills` tool is the fully-working, supported path for the same capability.

---

## Failure Modes to Prevent

- **No class confusion:** import `TypeSafeClient` from `typesafe_sdk` — never `JevClient` or the `typesafe_ai` module.
- **Choice criteria format:** `Choice(criteria={option: description})` is a **dictionary mapping**; a plain list (`Choice(options=...)`) raises a pydantic `ValidationError`.
- **Missing package errors:** always run inside `.venv` (`D:\mcp\jev-typesafe-mcp\.venv\Scripts\python.exe`).
- **No broken MCP import:** on mcp 2.x use `from mcp.server.mcpserver import MCPServer as FastMCP` (the `mcp.server.fastmcp` module is removed).
- **JSON-serializable returns:** every tool returns plain dicts (str / bool / float / None) — never pydantic objects.
- **Plugin isolation:** errors inside `jev-plugin.js` never propagate to OpenCode message hooks.

---

## Run & Verify

```powershell
cd D:\mcp\jev-typesafe-mcp

# Syntax check
& .\.venv\Scripts\python.exe -m py_compile jev_engine.py jev_mcp.py

# Import check
& .\.venv\Scripts\python.exe -c "import jev_mcp; print('MCP import successful!')"

# Engine CLI smoke test (live Jev API call)
& .\.venv\Scripts\python.exe jev_engine.py verify "git status"

# Print resolved jev_settings + scan dirs
& .\.venv\Scripts\python.exe -c "from jev_engine import load_jev_settings, get_scan_paths; from pathlib import Path; print(load_jev_settings()); print([str(p) for p in get_scan_paths(Path('.'))])"

# Start the server (blocks on stdio, waits for an MCP client)
& .\.venv\Scripts\python.exe jev_mcp.py
```

Expected outputs:

```
py_compile OK
MCP import successful!  (server: MCPServer)
{"safe": true, "destructive_prob": 0.01, "git_modify_prob": 0.01}
```

A `tools/list` handshake against a running `jev_mcp.py` returns exactly four tools:
`guardrail_command`, `search_agent_skills`, `search_target_files`, `select_model_tier`.

---

## Deploying the config (examples for GitHub)

The real OpenCode config files contain live API keys and live outside this repo, so the
repo carries **sanitized example copies** under `config/`:

| Example (committed) | Real install location |
|---|---|
| `config/opencode.example.json` | `C:\Users\<you>\.config\opencode\opencode.json` |
| `config/jev-plugin.example.js` | `C:\Users\<you>\.config\opencode\plugins\jev-plugin.js` |

See [`config/README.md`](config/README.md) for the full install guide.

The example intentionally omits the top-level `model` key so the default model comes
from your global OpenCode config (config files merge); add it back only to pin a
project-specific model — see `config/README.md`.

**Security:** never commit your real `opencode.json` or `.env` — both contain live keys
(`sk-...`, `apikey_...`). The `.gitignore` blocks `opencode.json` / `.opencode/` /
`config/opencode.local.json` so a real config can't accidentally slip into a commit.