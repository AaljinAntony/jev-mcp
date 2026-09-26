# Config Deployment

This folder contains **sanitized example copies** of the configuration and plugin
used with the `jev-engine` MCP server. Commit these examples to GitHub. The real
files live in your user profile or project root and must **never** be committed.

## File → destination mapping

| Example (committed) | Real install location |
|---|---|
| `config/opencode.example.json` | `C:\Users\<you>\.config\opencode\opencode.json` |
| `config/jevs_settings.example.json` | `<project-root>\jevs_settings.json` (per project) |
| `config/jev-plugin.example.js` | `C:\Users\<you>\.config\opencode\plugins\jev-plugin.js` |

## Install steps

1. **Copy the config example into place:**

   ```powershell
   Copy-Item config\opencode.example.json $env:USERPROFILE\.config\opencode\opencode.json
   ```

2. **Fill in the placeholders in `opencode.json`:**
   - `"<REPO_DIR>"` — the absolute path to this repository, in forward slashes:
     - the `command` array: `"<REPO_DIR>/.venv/Scripts/python.exe"` and `"<REPO_DIR>/jev_mcp.py"`

   Example for a repo at `D:\mcp\jev-typesafe-mcp`:

   ```jsonc
   "command": [
     "D:/mcp/jev-typesafe-mcp/.venv/Scripts/python.exe",
     "D:/mcp/jev-typesafe-mcp/jev_mcp.py"
   ]
   ```
   - `"environment"` uses `"{env:TYPESAFE_API_KEY}"` interpolation, so
     `TYPESAFE_API_KEY` must be set in your shell environment (see step 3).

   > OpenCode's `opencommand` schema is strict (`additionalProperties: false`):
   > unknown top-level keys (e.g. a `jev_settings` block) invalidate the **whole**
   > config and the MCP server silently disappears from `list`. Never add Jev
   > settings inside `opencode.json`.

3. **Configure the API key.** Two equivalent options:
   - Set `TYPESAFE_API_KEY` in your environment (used by the `"{env:TYPESAFE_API_KEY}"` interpolation), **or**
   - Create `.env` in the repo from `.env.example` (`Copy-Item .env.example .env`) — the server loads it as a fallback.

### Optional env knobs (`JEV_MCP_*`)

The MCP `environment` block in `config/opencode.example.json` pre-sets these;
values are read from `TYPESAFE_API_KEY` interpolation plus:

| Variable | Default | Meaning |
|---|---|---|
| `JEV_MCP_MODEL` | `jev-latest` | Model used for `system_one` calls. |
| `JEV_MCP_TIMEOUT_MS` | `30000` | **Total** per-tool-call budget in ms, retries and backoff included — not a per-attempt timeout. Enforced in mock mode too. |
| `JEV_MCP_ALLOWED_ROOTS` | *(empty)* | `os.pathsep`-separated extra directories an LLM-supplied `root_dir` may resolve inside. Empty = the process CWD and its ancestors only. |
| `JEV_MCP_BREAKER_THRESHOLD` | `3` | Consecutive provider failures after which the circuit breaker opens and later calls fail fast without a network round-trip. |
| `JEV_MCP_BREAKER_COOLDOWN_S` | `30` | Seconds an open breaker stays open before one probe call is allowed through. |
| `JEV_MCP_AUTH_COOLDOWN_S` | `300` | Cooldown after an auth failure (401/403). A bad key does not fix itself in 30 seconds. |
| `JEV_MCP_MOCK` | `0` | `1` = offline deterministic judge (tests/demos only). |
| `JEV_MCP_AUTO_ACCEPT` | `0.8` | Confidence at or above which a decision is `auto`. |
| `JEV_MCP_REVIEW_AT` | `0.5` | Confidence below which a decision `escalate`s. |
| `JEV_MCP_LOG_FILE` | `<repo>/logs/jev_engine.log` | Absolute path to the server log; each tool call and provider round is recorded (always written cwd-independently). |

Thresholds must satisfy `0 <= review_at <= auto_accept <= 1` (invalid values
fail fast with a `CONFIG_ERROR` envelope instead of silently mis-routing).

### `root_dir` and the allowed-roots allowlist

`root_dir` is **confined to an allowlist**, not screened against a denylist — a
denylist cannot enumerate every sensitive path, and an LLM-supplied `root_dir`
should carry no more privilege than the session's own working directory.

Allowed:

- the process working directory, and anything under it;
- any **ancestor** of the working directory (hosts launch the server with `cwd`
  set to the project root or to a temp dir);
- anything under a path listed in `JEV_MCP_ALLOWED_ROOTS`.

Everything else is rejected with `INVALID_INPUT`. Containment is computed with
`Path.relative_to`, never `startswith`, so `<root>-evil` and a symlink pointing
outside the root are both refused. The OS's own directories (`C:\Windows`,
`C:\Program Files`, a filesystem root) are still rejected by name as a second
gate, even if you list them.

To let the server read a project you are working on from a different directory,
allowlist it explicitly:

```powershell
$env:JEV_MCP_ALLOWED_ROOTS="D:\work\other-project;D:\work\shared"
```

4. **(Optional) per-project Jev settings:** copy
   `config\jevs_settings.example.json` to your project root as `jevs_settings.json`
   and fill in the model IDs. If the file is absent, built-in defaults are used.

5. **(Optional) plugin:** copy `config\jev-plugin.example.js` to
   `$env:USERPROFILE\.config\opencode\plugins\jev-plugin.js`.
   The plugin never writes to the conversation; it logs diagnostics to
   `~\.config\opencode\logs\jev-plugin.log`.

6. **Restart OpenCode** so the MCP server and settings are re-read, then run the
   verification commands from the main `README.md`.

## Troubleshooting

"Unexpected error occurred" while sending a prompt in a project that has a
`.agents/` folder? The fix below is already applied to the installed plugin
(`C:\Users\<you>\.config\opencode\plugins\jev-plugin.js`):

- The `chat.message` hook must **never** block the opencode process. The plugin's
  Jev query is now an **async** `spawn` + `await` (4 s cap), not a synchronous
  `spawnSync`. A blocking call inside the hook is what trips opencode into the
  error popup and task auto-stop.
- Check the traces to see who failed:
  - `~\.config\opencode\logs\jev-plugin.log` — hook fired? candidates found? spawn result?
  - `<repo>\logs\jev_engine.log` — was the tool even invoked? current tool call, duration, envelope.
- Reproduce a tool failure over the real transport (same as opencode) with:

  ```powershell
  &.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "fix ui bug" --root_dir D:\Godot_projects\flux-wall
  ```

### Default model (optional)

The top-level `"model"` key is intentionally omitted from the example. OpenCode
**merges** config files (global `~/.config/opencode/opencode.json` → project config),
so without a `"model"` key here, the model set in your global config is used
automatically. The `"model"` key is **not** a Jev setting — it only affects what
opencode uses when model routing is **off**.

To set a project-specific default, add it back:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "provider/model-id",   // overrides the global model for this project
  "mcp": { ... }
}
```

> ⚠️ A present `"model"` **overrides** the global config's model — there is no
> fallback to the global value if the ID is invalid. Only set it if you want to pin
> this project to a specific model.

## `jevs_settings` reference (`jevs_settings.json`)

```jsonc
{
  "enable_model_routing": false,   // master switch: off = no model switching (window model used)
  "models": {
    "fast":     "",                 // tier -> "provider/model-id" used for FORCED switching
    "balanced": "",
    "frontier": ""
  },
  "scan_paths": []                  // extra skill dirs, appended to the defaults
}
```

- Lookup order: `<project>/jevs_settings.json` →
  `<project>/.opencode/jevs_settings.json` →
  `~/.config/opencode/jevs_settings.json` → legacy `jev_settings` block in
  `opencode.json` (project > user). Discovery order is unchanged; **precedence
  is applied by merging**: the user-level files are applied first, then the
  project files override them **per key**. Neither discards the other, so a
  project file that only sets `enable_model_routing` no longer wipes out the
  user file's `models` map.
- `opencode.json` is only read through an explicit `jev_settings` block; its
  unrelated top-level keys are never treated as Jev settings.
- All keys optional; missing keys fall back to defaults: routing **off**, empty
  `models`, built-in `scan_paths` (`.agents/skills`, `.agents/workflows`,
  `.agents/memory`, `.opencode/skills`, `skills`, `.agents`).
- `scan_paths` entries are relative to the workspace root and **appended** to the
  built-in defaults, deduplicated. They union across files.
- `models` entries **union** non-empty values across files, so a project can add
  a tier without deleting the user's others. An explicit `""` **removes** an
  inherited tier — that is how a tier is disabled, and it is no longer
  equivalent to omitting the key once a user-level file has set it.
- `load_jev_settings()["source"]` is the last contributor (the project file);
  `["sources"]` lists every file that contributed. The returned dict is a
  defensive copy — mutating it cannot corrupt the cache.

### What "routing on" actually does

When `enable_model_routing` is **on** and all three tiers have model IDs, the
`jev-plugin.js` `chat.message` hook asks Jev the tier, looks up the model ID, and
**forces** the switch by mutating `output.message.model` (opencode uses that value
for the reply — a hard switch, not a recommendation). `select_model_tier` remains
available as an MCP tool for explicit/on-demand queries. When routing is **off**
(the default), nothing is changed and opencode uses its `"model"` config /
window-selected model.

## Security

- **Never commit** your real `opencode.json` or `.env` — they contain live API keys (`sk-...`, `apikey_...`).
- `jevs_settings.json` holds only model IDs (no secrets) — it is safe to commit, e.g. to share a team default; the root `.gitignore` does not block it.
- If a real key was ever pushed publicly, treat it as compromised and rotate it.
- `.env.example` is safe to commit (placeholder only).