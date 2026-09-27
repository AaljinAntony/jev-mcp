# Config Deployment

This folder contains **sanitized example copies** of the configuration and plugin
used with the `jev-engine` MCP server. Commit these examples to GitHub. The real
files live in your user profile or project root and must **never** be committed.

## File → destination mapping

| Example (committed) | Real install location |
|---|---|
| `config/opencode.example.json` | `C:\Users\<you>\.config\opencode\opencode.json` |
| `config/antigravity.example.json` | `C:\Users\<you>\.gemini\config\mcp_config.json` |
| `config/jevs_settings.example.json` | `<project-root>\jevs_settings.json` (per project) |
| `config/jev-plugin.example.js` | `C:\Users\<you>\.config\opencode\plugins\jev-plugin.js` |

## The plugin's lifecycle

The plugin is a **copy**, not a symlink or an import, and it is the only file
opencode loads. That makes drift the failure mode to design against:

```
config/jev-plugin.example.js        <- edited here, committed
        │  node tests\test_plugin.mjs      (behaviour + drift check)
        │  Copy-Item ... -Force
        ▼
~\.config\opencode\plugins\jev-plugin.js   <- what opencode actually runs
        ▲
        │  scripts\doctor.py               (same SHA256 comparison, no Node)
        │
```

Rules that follow from that:

- **Edit the example, never the installed copy.** An edit made only in the
  installed file is lost on the next reinstall and is invisible to review.
- **The example is the single server implementation.** The plugin holds no
  Python and no TypeSafe SDK access; it drives the same `jev_mcp.py` over stdio
  JSON-RPC as an MCP client. There is no second code path to keep in step, and
  the plugin tests run against a real child process for exactly that reason.
- **Copy, then test, then restart.** opencode loads the plugin once at startup;
  a copied file has no effect until it restarts.
- **The drift check is not optional.** `node tests\test_plugin.mjs` ends with a
  byte comparison against the installed file and fails on any difference. A
  427-line installed plugin that was missing every guard the example has is a
  real event from this project's history.

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
   - Do **not** put `TYPESAFE_API_KEY` in `"environment"`. opencode resolves
     `"TYPESAFE_API_KEY": "{env:TYPESAFE_API_KEY}"` to an **empty string** when
     that variable is missing from its own environment, and it passes the empty
     string to the child — which then shadows the repo `.env` and every live
     tool call fails with `TYPESAFE_API_KEY environment variable is not
     configured`. The server finds `.env` relative to its own module, so no
     `environment` entry is needed for the key in any workspace.

   > OpenCode's `opencommand` schema is strict (`additionalProperties: false`):
   > unknown top-level keys (e.g. a `jev_settings` block) invalidate the **whole**
   > config and the MCP server silently disappears from `list`. Never add Jev
   > settings inside `opencode.json`.

3. **Configure the API key.** Create `.env` in the repo from `.env.example`
   (`Copy-Item .env.example .env`). The server loads it at startup, relative to
   its own module, so the key resolves in every workspace. A `TYPESAFE_API_KEY`
   already present in the server's environment still wins — a blank one is
   treated as absent.

### Antigravity IDE setup

1. **Add `jev-engine` to Antigravity's `mcp_config.json`:**
   Open `C:\Users\<you>\.gemini\config\mcp_config.json` and register the server under `mcpServers`:
   ```json
   {
     "mcpServers": {
       "jev-engine": {
         "command": "<REPO_DIR>\\.venv\\Scripts\\python.exe",
         "args": [
           "<REPO_DIR>\\jev_mcp.py"
         ]
       }
     }
   }
   ```
   (See `config/antigravity.example.json` for the template).

2. **Configure allowed workspace roots:**
   Because Antigravity executes the MCP server from the repository root rather than the active workspace, add your project roots to `JEV_MCP_ALLOWED_ROOTS` in `.env`:
   ```dotenv
   JEV_MCP_ALLOWED_ROOTS=D:\Projects;D:\Godot_projects;D:\mcp
   ```

3. **Restart / reload Antigravity:**
   Reload the window or restart the IDE to pick up the new MCP server.


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
| `JEV_MCP_LOG_PREVIEW` | `0` | `1` = also log a 1,000-character `result_preview` per tool call. Off by default: the result is serialized twice (here and by the MCP runtime) and a skill result carries kilobytes of file content. The preview is redacted like every other logged value. |
| `JEV_PLUGIN_SCAN_ROOTS` | *(empty)* | Read by the **plugin**, not the server. Extra skill directories outside the project; forwarded to the child as `JEV_MCP_ALLOWED_ROOTS` so both sides agree on what may be read. |

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
   `~\.config\opencode\logs\jev-plugin.log` (rotating at 2 MB, and never
   containing prompt text — only its length and a short digest).
   It reuses the same `jev_mcp.py` you configured above, spawned once per
   session and driven as a stdio MCP client, so no second server implementation
   exists to drift.

   To keep skills outside the project, opt in explicitly (forwarded to the
   server as `JEV_MCP_ALLOWED_ROOTS`):

   ```powershell
   $env:JEV_PLUGIN_SCAN_ROOTS="$env:USERPROFILE\.config\opencode\skills"
   ```

6. **(Required after any plugin edit) run the plugin tests:**

   ```powershell
   node tests\test_plugin.mjs
   ```

   The last check in that file compares
   `~\.config\opencode\plugins\jev-plugin.js` against
   `config\jev-plugin.example.js` byte for byte and **fails** on any difference.
   A 427-line installed plugin missing every guard the example has is exactly
   the failure this catches, so treat the copy and the test as one step: edit the
   example, copy it, run the test. `scripts\doctor.py` runs the same SHA256
   comparison, so a user without Node can check it too.

7. **Restart OpenCode** so the MCP server, the settings and the plugin are
   re-read.

8. **Check the installation:**

   ```powershell
   & .\.venv\Scripts\python.exe scripts\doctor.py
   ```

   Read-only; no child process, no API call. It verifies the interpreter, both
   SDKs, key *presence* (length and a 4-character suffix only), settings
   resolution and their `sources`, the `root_dir` allowlist against the current
   directory, log-directory writability, `review_at <= auto_accept`, and the
   plugin copy from step 6. Exits 0 when healthy, 1 otherwise, with a one-line
   fix per finding. Run it **before** reading any trace below.

## Troubleshooting

Start with `scripts\doctor.py` (step 8). If it is clean, the remaining failures
are behavioural, and these are the two that have actually happened:

"Unexpected error occurred" while sending a prompt in a project that has a
`.agents/` folder? The fix below is already applied to the installed plugin
(`C:\Users\<you>\.config\opencode\plugins\jev-plugin.js`):

- The `chat.message` hook must **never** block the opencode process. The plugin's
  Jev query is a **non-blocking** async stdio JSON-RPC round trip against a
  long-lived `jev_mcp.py` child (20 s per-request cap), not a synchronous spawn. A
  blocking call inside the hook is what trips opencode into the error popup and
  task auto-stop. Injecting a skill **edits the existing user text part** rather
  than pushing a new one, because opencode validates every part against `PartV2`
  at save time and a bare `{type:"text", text}` crashes `createUserMessage`.
- Check the traces to see who failed:
  - `~\.config\opencode\logs\jev-plugin.log` — hook fired? `client started`?
    per-tool `action`/`confidence`? injected or skipped, and why? A second
    message must **not** log a second `client started`: that is the
    connection-reuse proof. A parallel window that logs
    `skip: 3 jev queries already in flight` is at the concurrency cap, and
    `result dropped: superseded by a newer message` is the per-session
    staleness guard doing its job — neither is an error.
    `no skill to inject: action=auto confidence=0.99 primary=none` means the
    judge answered "nothing here applies", not that the plugin failed.
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
  "scan_paths": [],                 // extra skill dirs, appended to the defaults
  "judge_read_prompts": true,       // PLUGIN: judge a prompt file the agent reads
  "inject_agent_instructions": true // PLUGIN: state the decision points every turn
}
```

- Lookup order: `<project>/jevs_settings.json` →
  `<project>/.opencode/jevs_settings.json` →
  `<repo>/jevs_settings.json` (the server's own directory, so the CWD does not
  matter) → `~/.config/opencode/jevs_settings.json`. Discovery order is
  unchanged; **precedence is applied by merging**: the user-level files are
  applied first, then the project files override them **per key**. Neither
  discards the other, so a project file that only sets `enable_model_routing` no
  longer wipes out the user file's `models` map.
- `opencode.json` is **not** a settings source, for the reason in step 2: its
  schema rejects unknown top-level keys, so a `jev_settings` block would stop the
  MCP server from loading at all. The lookup that used to read one has been
  deleted from the server.
- All keys optional; missing keys fall back to defaults: routing **off**, empty
  `models`, built-in `scan_paths` (`.agents/skills`, `.agents/workflows`,
  `.agents/memory`, `.opencode/skills`, `skills`, `.agents`).
- `scan_paths` entries are relative to the workspace root and **appended** to the
  built-in defaults, deduplicated. They union across files. A scan path that is
  already inside another configured scan path (the built-in `.agents` covers
  `.agents/skills`, `.agents/workflows` and `.agents/memory`) is collapsed to the
  ancestor, so those files are walked once instead of four times.
- `judge_read_prompts` and `inject_agent_instructions` are read by the **plugin
  only**, are **on by default**, and are disabled only by an explicit `false` — a
  typo must not be able to switch the judge off.
- `models` entries **union** non-empty values across files, so a project can add
  a tier without deleting the user's others. An explicit `""` **removes** an
  inherited tier — that is how a tier is disabled, and it is no longer
  equivalent to omitting the key once a user-level file has set it.
- `load_jev_settings()["source"]` is the last contributor (the project file);
  `["sources"]` lists every file that contributed. The returned dict is a
  defensive copy — mutating it cannot corrupt the cache.

### What "routing on" actually does

When `enable_model_routing` is **on** and at least one tier has a model ID, the
`jev-plugin.js` `chat.message` hook asks Jev the tier, looks up the model ID, and
**forces** the switch by mutating `output.message.model` (opencode uses that value
for the reply — a hard switch, not a recommendation). A partial `models` map is
fine: if the tier Jev picks has no ID, nothing is switched and the hook logs why.
`select_model_tier` remains available as an MCP tool for explicit/on-demand
queries. When routing is **off** (the default), nothing is changed and opencode
uses its `"model"` config / window-selected model.

The two effects have **different** gates, which is the part worth remembering:

| effect | gate | why |
|---|---|---|
| skill injection | `action` is `auto` **or** `review`, and `confidence >= 0.6` | a skill note is advisory text in the user's own message, not an action |
| model switch | `action` is `auto` and `confidence >= 0.6`, plus a well-formed `provider/model` id and an existing `output.message.model` | moving the model changes how the reply is produced, so it takes the server's unreserved verdict |

Requiring `auto` for the injection, as this plugin used to, made the documented 0.6
floor unreachable: the server only says `auto` at `JEV_MCP_AUTO_ACCEPT` (0.8), so
the real bar was 0.8 and every decision in between was dropped without a trace.
`escalate` is refused by both — below `JEV_MCP_REVIEW_AT` (0.5) the model is
guessing among options that do not fit.

## Security

- **Never commit** your real `opencode.json` or `.env` — they contain live API keys (`sk-...`, `apikey_...`).
- `jevs_settings.json` holds only model IDs (no secrets) — it is safe to commit, e.g. to share a team default; the root `.gitignore` does not block it.
- If a real key was ever pushed publicly, treat it as compromised and rotate it.
- `.env.example` is safe to commit (placeholder only).