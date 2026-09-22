# Config Deployment

This folder contains **sanitized example copies** of the OpenCode configuration and
plugin used with the `jev-engine` MCP server. Commit these examples to GitHub. The
real files live in your user profile and must **never** be committed.

## File → destination mapping

| Example (committed) | Real install location |
|---|---|
| `config/opencode.example.json` | `C:\Users\<you>\.config\opencode\opencode.json` |
| `config/jev-plugin.example.js` | `C:\Users\<you>\.config\opencode\plugins\jev-plugin.js` |

## Install steps

1. **Copy the config example into place:**

   ```powershell
   Copy-Item config\opencode.example.json $env:USERPROFILE\.config\opencode\opencode.json
   ```

2. **Fill in the placeholders in `opencode.json`:**
   - `"<REPO_DIR>"` — the absolute path to this repository, in forward slashes. Both occurrences:
     - `command`: `"<REPO_DIR>/.venv/Scripts/python.exe"`
     - `args`: `"<REPO_DIR>/jev_mcp.py"`

   Example for a repo at `D:\mcp\jev-typesafe-mcp`:

   ```jsonc
   "command": "D:/mcp/jev-typesafe-mcp/.venv/Scripts/python.exe",
   "args":     ["D:/mcp/jev-typesafe-mcp/jev_mcp.py"]
   ```

### Default model (optional)

The top-level `"model"` key is intentionally omitted from the example. OpenCode
**merges** config files (global `~/.config/opencode/opencode.json` → project config),
so without a `"model"` key here, the model set in your global config (e.g.
`omniroute/free-stack`) is used automatically.

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

3. **Configure the API key.** Two equivalent options:
   - Set `TYPESAFE_API_KEY` in your environment (used by the `"${TYPESAFE_API_KEY}"` interpolation), **or**
   - Create `.env` in the repo from `.env.example` (`Copy-Item .env.example .env`) — the server loads it as a fallback.

4. **(Optional) plugin:** copy `config\jev-plugin.example.js` to
   `$env:USERPROFILE\.config\opencode\plugins\jev-plugin.js`.

   > ⚠️ The plugin's inline Python still targets the legacy `JevClient` API and does
   > not run against `typesafe-sdk==0.7.1`. It fails gracefully. Use the MCP tool
   > `search_agent_skills` for the supported path.

5. **Restart OpenCode** so the MCP server and `jev_settings` are re-read, then run the
   verification commands from the main `README.md`.

## `jev_settings` reference

```jsonc
"jev_settings": {
  "enable_model_routing": false,   // master switch for select_model_tier (off = no Jev call)
  "models": {
    "fast":     "",                 // tier -> model ID mapping
    "balanced": "",
    "frontier": ""
  },
  "scan_paths": []                  // extra skill dirs, appended to the defaults
}
```

- Project-level `opencode.json` / `.opencode/opencode.json` take priority over the
  user-level file when both contain a `jev_settings` block.
- `scan_paths` entries are relative to the workspace root and merged with the built-in
  defaults (`.agents/skills`, `.agents/workflows`, `.agents/memory`,
  `.opencode/skills`, `skills`, `.agents`), deduplicated.

## Security

- **Never commit** your real `opencode.json` or `.env` — they contain live API keys
  (`sk-...`, `apikey_...`).
- If a real key was ever pushed publicly, treat it as compromised and rotate it.
- `.env.example` is safe to commit (placeholder only).