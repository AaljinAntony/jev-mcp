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
| `JEV_MCP_TIMEOUT_MS` | `30000` | Per-request timeout in ms. |
| `JEV_MCP_MOCK` | `0` | `1` = offline deterministic judge (tests/demos only). |
| `JEV_MCP_AUTO_ACCEPT` | `0.8` | Confidence at or above which a decision is `auto`. |
| `JEV_MCP_REVIEW_AT` | `0.5` | Confidence below which a decision `escalate`s. |

Thresholds must satisfy `0 <= review_at <= auto_accept <= 1` (invalid values
fail fast with a `CONFIG_ERROR` envelope instead of silently mis-routing).

4. **(Optional) per-project Jev settings:** copy
   `config\jevs_settings.example.json` to your project root as `jevs_settings.json`
   and fill in the model IDs. If the file is absent, built-in defaults are used.

5. **(Optional) plugin:** copy `config\jev-plugin.example.js` to
   `$env:USERPROFILE\.config\opencode\plugins\jev-plugin.js`.

6. **Restart OpenCode** so the MCP server and settings are re-read, then run the
   verification commands from the main `README.md`.

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
  `opencode.json` (project > user). The first file that contains any Jev key wins.
- All keys optional; missing keys fall back to defaults: routing **off**, empty
  `models`, built-in `scan_paths` (`.agents/skills`, `.agents/workflows`,
  `.agents/memory`, `.opencode/skills`, `skills`, `.agents`).
- `scan_paths` entries are relative to the workspace root and **appended** to the
  built-in defaults, deduplicated.
- Empty `models` values are ignored, so `""` placeholders are equivalent to omitting
  the tier until you're ready to enable routing.

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