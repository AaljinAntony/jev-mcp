# Configuration templates

Sanitized example copies of every config this server needs. **Commit these
examples; never commit the real files.** The real ones live in your user profile
or your project root and may contain live API keys.

Every example uses the placeholder `<REPO_DIR>` — the absolute path to your clone
of this repository. Replace it before copying.

## Which example, which destination

There are only **five config dialects** across every harness. Pick your harness
from the table, then use the block for its dialect.

| Harness | Dialect | Install to |
|---|---|---|
| **OpenCode** | `mcp` array (JSON) | `~/.config/opencode/opencode.json` |
| **Claude Code** | `mcpServers` (JSON) | `.mcp.json` in the project, or `~/.claude.json` |
| **Cursor** | `mcpServers` (JSON) | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` |
| **Antigravity** | `mcpServers` (JSON) | `~/.gemini/config/mcp_config.json` |
| **Claude Desktop** | `mcpServers` (JSON) | `%APPDATA%\Claude\claude_desktop_config.json` |
| **VS Code / Copilot** | `servers` (JSON) | `.vscode/mcp.json` in the workspace |
| **Codex CLI** | TOML | `~/.codex/config.toml` |
| **Hermes Agent** | YAML | `~/.hermes/config.yaml` |
| *per-project Jev settings* | — | `<project-root>/jevs_settings.json` |
| *OpenCode plugin (optional)* | JS | `~/.config/opencode/plugins/jev-plugin.js` |

Examples in this folder:

| Example (committed) | Dialect | Real install location |
|---|---|---|
| [`opencode.example.json`](opencode.example.json) | `mcp` array | `~/.config/opencode/opencode.json` |
| [`claude-code.example.json`](claude-code.example.json) | `mcpServers` | `<project>/.mcp.json` or `~/.claude.json` |
| [`cursor.example.json`](cursor.example.json) | `mcpServers` | `<project>/.cursor/mcp.json` |
| [`antigravity.example.json`](antigravity.example.json) | `mcpServers` | `~/.gemini/config/mcp_config.json` |
| [`vscode.example.json`](vscode.example.json) | `servers` | `<workspace>/.vscode/mcp.json` |
| [`codex.example.toml`](codex.example.toml) | TOML | `~/.codex/config.toml` |
| [`hermes.example.yaml`](hermes.example.yaml) | YAML | `~/.hermes/config.yaml` |
| [`jevs_settings.example.json`](jevs_settings.example.json) | Jev settings | `<project-root>/jevs_settings.json` |
| [`jev-plugin.example.js`](jev-plugin.example.js) | OpenCode plugin | `~/.config/opencode/plugins/jev-plugin.js` |

---

## The API key goes in `.env`, never in the harness config

This is the single most important rule in this file.

```powershell
Copy-Item .env.example .env
# then edit .env and set TYPESAFE_API_KEY=<your key>
```

The server loads `.env` with `python-dotenv` **relative to its own module
directory**, not the working directory. One `.env` in the repo therefore serves
every workspace, every harness and every session.

Do **not** add `TYPESAFE_API_KEY` to a harness `environment` / `env` block.
OpenCode resolves `"TYPESAFE_API_KEY": "{env:TYPESAFE_API_KEY}"` to an **empty
string** when that variable is missing from its own environment, and
`python-dotenv` treats "already present" as authoritative even for `""` — which
shadows the repo `.env` and makes every live call fail with
`TYPESAFE_API_KEY environment variable is not configured`.

An injected value always wins over the file, so a stale `.env` cannot override a
deliberate setting. The one exception is a *blank* injected value, which is
treated as absent so the file can supply it. If your harness insists on passing
the key explicitly, forward it as a **name** (Codex's `env_vars`) rather than a
literal value.

---

## Install steps

### 1. The repository

```powershell
git clone https://github.com/AaljinAntony/jev-mcp.git
cd jev-mcp
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
#   ./.venv/bin/python -m pip install -r requirements.txt         # macOS / Linux
Copy-Item .env.example .env
```

> **On Debian/Ubuntu**, `python3 -m venv .venv` fails with *"You may need to use
> sudo with that command"* unless the separate `python3-venv` package is
> installed (`sudo apt install python3-venv`). Ubuntu packages `ensurepip`
> separately from Python itself. Without `sudo`, install `virtualenv` instead —
> `pip install --user virtualenv && virtualenv .venv` — which bundles its own
> pip and needs no system package.

Edit `.env` and set `TYPESAFE_API_KEY`. Then:

```powershell
& .\.venv\Scripts\python.exe scripts\doctor.py
```

`doctor.py` is read-only — it writes nothing and makes no API call. Its one child
process is `opencode --version`, and only so the MCP config can be checked against
the schema your installed version actually uses; that step is skipped when
opencode is not on `PATH`. It checks the
interpreter, both SDKs, key *presence* (length and a 4-character suffix only,
never the value), settings resolution and their `sources`, the `root_dir`
allowlist against the current directory, log-directory writability,
`review_at <= auto_accept`, and whether the installed plugin matches the
example by SHA256.

It also validates the **OpenCode MCP config** itself: it finds the config the
host would load, reads the `jev-engine` entry out of either OpenCode config
shape, checks that the `command` paths exist and contain no unsubstituted
`<REPO_DIR>` placeholder, and — when `opencode` is on `PATH` — checks the config
shape against the installed version. That last step exists because this failure
is silent: a config the running version does not accept makes the server vanish
from the tool list instead of raising.

Exits 0 when healthy, 1 otherwise, with a one-line fix per
finding. **Run it before reading any other trace.**

### 2. Your harness

Copy the example for your dialect, replace `<REPO_DIR>` with the absolute path to
the clone, and place it where the table above says. Forward slashes are safest
inside JSON on every platform.

OpenCode — this substitutes `<REPO_DIR>` for you:

```powershell
$repo = (Resolve-Path .).Path.Replace('\', '/')
(Get-Content config\opencode.example.json -Raw).Replace('<REPO_DIR>', $repo) |
  Set-Content "$env:USERPROFILE\.config\opencode\opencode.json"
```

```bash
repo=$(pwd | sed 's|\\|/|g')
sed "s|<REPO_DIR>|$repo|g" config/opencode.example.json \
  > ~/.config/opencode/opencode.json
```

> **Both commands replace the whole file.** If you already have an
> `opencode.json` — a provider block, other MCP servers — merge the `mcp` entry by
> hand instead of running this. A plain `Copy-Item` of the example, with
> `<REPO_DIR>` left in place, is the single most likely way to end up with no
> server at all: the spawn fails and OpenCode drops the entry without saying so.
> `scripts/doctor.py` names both problems explicitly — run it from step 1.

Claude Code, Cursor, Antigravity, VS Code, Codex CLI and Hermes Agent all use the
same two steps with a different source and destination — see the table.

Then restart the harness. OpenCode, Cursor and Antigravity read config at
startup; VS Code shows a **Start** button in `.vscode/mcp.json`; Codex and
Hermes read on next launch.

### 3. Optional — per-project Jev settings

```powershell
Copy-Item config\jevs_settings.example.json <your-project>\jevs_settings.json
```

Holds only model IDs, no secrets, so it **is** safe to commit to share a team
default. See [`jevs_settings` reference](#jevs_settings-reference).

### 4. Optional — the OpenCode plugin

Only OpenCode has a plugin API, so only OpenCode gets the automatic behaviour:
skill injection, judged prompt files, the system-prompt rules block, and **forced
model routing**.

```powershell
Copy-Item config\jev-plugin.example.js $env:USERPROFILE\.config\opencode\plugins\jev-plugin.js
```

The plugin is a **copy**, not a symlink or an import, and it is the only file
OpenCode loads. Drift is therefore the failure mode to design against:

```
config/jev-plugin.example.js        <- edit here, commit
        |  node tests/test_plugin.mjs      (behaviour + drift check)
        |  Copy-Item ... -Force
        v
~/.config/opencode/plugins/jev-plugin.js   <- what OpenCode actually runs
        |
        '  scripts/doctor.py               (same SHA256 comparison, no Node needed)
```

Rules that follow:

- **Edit the example, never the installed copy.** An edit made only in the
  installed file is lost on the next reinstall and is invisible to review.
- **The example is the single implementation.** The plugin holds no Python and no
  TypeSafe SDK access; it drives the same `jev_mcp.py` over stdio JSON-RPC as an
  MCP client. There is no second code path to keep in step.
- **Copy, then test, then restart.** OpenCode loads the plugin once at startup; a
  copied file has no effect until it restarts.
- **The drift check is not optional.** `node tests/test_plugin.mjs` ends with a
  byte comparison against the installed file and fails on any difference. A
  427-line installed plugin missing every guard the example has is a real event
  from this project's history.

To keep skills outside the project, opt in explicitly:

```powershell
$env:JEV_PLUGIN_SCAN_ROOTS="$env:USERPROFILE\.config\opencode\skills"
```

The plugin forwards that to the server as `JEV_MCP_ALLOWED_ROOTS`, so both sides
agree on what may be read.

---

## `root_dir` and the allowed-roots allowlist

`root_dir` is **confined to an allowlist**, not screened against a denylist — a
denylist cannot enumerate every sensitive path, and an LLM-supplied `root_dir`
should carry no more privilege than the session's own working directory.

Allowed:

- the process working directory, and anything under it;
- any **ancestor** of the working directory that sits **below your home
  directory** — hosts launch the server with `cwd` set below the project root,
  and a session legitimately asks about a parent of that;
- anything under a path listed in `JEV_MCP_ALLOWED_ROOTS`.

Everything else is rejected with `INVALID_INPUT`. Containment is computed with
`Path.relative_to`, never `startswith`, so `<root>-evil` and a symlink pointing
outside the root are both refused. The OS's own directories are refused by name
as a second gate, even if you list them: `C:\Windows`, `C:\Program Files` and a
drive root on Windows; `/etc`, `/proc`, `/sys`, `/dev`, `/var`, `/opt`, `/srv`
and `/root` on Linux and macOS.

**The ancestor walk stops at two boundaries, and both matter.** It never reaches
the filesystem root, because a base of `/` would make *every* absolute path on
the machine a member. And it never reaches `$HOME` or above, because the usual
checkout (`~/code/project`) puts your whole home directory among the
ancestors. The working directory itself is always allowed, even when it *is*
`$HOME` — that is the one directory the session already has.

`/home` and `/Users` are deliberately not in the system-directory veto: they
are not system trees, they are where work lives, and your checkout is normally
under one.

Most harnesses launch the server with the project as the working directory, so
nothing is needed. **Antigravity is the exception** — it executes the MCP server
from the repository root rather than the active workspace, so allowlist your
project roots:

```dotenv
JEV_MCP_ALLOWED_ROOTS=/home/you/code/project-one;/home/you/code/project-two
```

The separator is `os.pathsep` — `;` on Windows, `:` on POSIX. Relative and
non-existent entries are ignored.

---

## Optional env knobs (`JEV_MCP_*`)

Read by the **server**:

| Variable | Default | Meaning |
|---|---|---|
| `TYPESAFE_API_KEY` | *(required)* | Provider key. Missing + `JEV_MCP_MOCK=0` → `CONFIG_ERROR`. |
| `JEV_MCP_MODEL` | `jev-latest` | Model used for `system_one` calls. |
| `JEV_MCP_TIMEOUT_MS` | `30000` | **Total** per-tool-call budget in ms, retries and backoff included — not a per-attempt timeout. Enforced in mock mode too. |
| `JEV_MCP_ALLOWED_ROOTS` | *(empty)* | `os.pathsep`-separated extra directories an LLM-supplied `root_dir` / `task_file` may resolve inside. Empty = the process CWD and its ancestors below `$HOME` only. |
| `JEV_MCP_GLOBAL_SCAN_PATHS` | *(empty)* | `os.pathsep`-separated extra directories of globally installed skills, **added** to the per-user ones the server always scans. Entries that do not exist are ignored. |
| `JEV_MCP_AUTO_ACCEPT` | `0.8` | Confidence at or above which a decision is `auto`. |
| `JEV_MCP_REVIEW_AT` | `0.5` | Confidence below which a decision `escalate`s. |
| `JEV_MCP_MOCK` | `0` | `1` = offline deterministic judge (tests/demos only). |
| `JEV_MCP_BREAKER_THRESHOLD` | `3` | Consecutive provider failures after which the breaker opens and later calls fail fast without a network round-trip. |
| `JEV_MCP_BREAKER_COOLDOWN_S` | `30` | Seconds an open breaker stays open before one probe call is allowed through. |
| `JEV_MCP_AUTH_COOLDOWN_S` | `300` | Cooldown after an auth failure (401/403). A bad key does not fix itself in 30 seconds. |
| `JEV_MCP_LOG_FILE` | `<repo>/logs/jev_engine.log` | Absolute path to the server log. Always written cwd-independently. |
| `JEV_MCP_LOG_PREVIEW` | `0` | `1` = also log a 1 000-character `result_preview` per tool call. Off by default: the result is serialized twice and a skill result carries kilobytes of file content. The preview is redacted like every other logged value. |

Read by the **plugin** only:

| Variable | Default | Meaning |
|---|---|---|
| `JEV_PLUGIN_SCAN_ROOTS` | *(empty)* | Extra skill directories outside the project; forwarded to the child as `JEV_MCP_ALLOWED_ROOTS` so both sides agree on what may be read. |
| `JEV_PLUGIN_LOG_DIR` | `~/.config/opencode/logs` | Plugin log directory. |
| `JEV_PLUGIN_ALLOW_OUTSIDE` | *(unset)* | `=1` disables the plugin's own workspace containment on `scan_paths`. |

Thresholds must satisfy `0 <= review_at <= auto_accept <= 1`. An invalid value
fails fast with a `CONFIG_ERROR` envelope instead of silently mis-routing.

---

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
  "ignore_mcps": [],                // MCP names/globs `select_mcp_tools` never offers
  "judge_read_prompts": true,       // PLUGIN: judge a prompt file the agent reads
  "inject_agent_instructions": true // PLUGIN: state the decision points every turn
}
```

- Lookup order: `<project>/jevs_settings.json` →
  `<project>/.opencode/jevs_settings.json` →
  `<repo>/jevs_settings.json` (the server's own directory, so the CWD does not
  matter) → `~/.config/opencode/jevs_settings.json`. Discovery order is
  unchanged; **precedence is applied by merging**: user-level files first, then
  project files override **per key**. Neither discards the other, so a project
  file that only sets `enable_model_routing` no longer wipes out the user file's
  `models` map.
- `opencode.json` is **not** a settings source. Its schema rejects unknown
  top-level keys, so a `jev_settings` block there would stop the MCP server from
  loading at all.
- All keys optional; missing keys fall back to defaults: routing **off**, empty
  `models`, built-in `scan_paths` (`.agents/skills`, `.agents/workflows`,
  `.agents/memory`, `.opencode/skills`, `skills`, `.agents`) and an
  `ignore_mcps` of `["jev-engine*"]`.
- `scan_paths` entries are relative to the workspace root and **appended** to the
  built-in defaults, deduplicated. A scan path already inside another configured
  scan path is collapsed to the ancestor, so those files are walked once instead
  of four times. An **absolute** entry is read as-is, which is how a project points
  at a directory outside itself.
- Independently of `scan_paths`, the per-user directories `~/.agents/skills`,
  `~/.agents/workflows`, `~/.agents/memory` and `~/.config/opencode/skills` are
  scanned on every call, plus anything in `JEV_MCP_GLOBAL_SCAN_PATHS`. They are
  derived from the home directory, so nothing machine-specific goes in a settings
  file and a new machine needs no edit. Files from these directories are reported
  under a `~/`-shortened key when they live under home, and under their absolute
  path otherwise. Workspace scan paths are walked first, so the discovery cap
  keeps project skills in preference to global ones.
- `ignore_mcps` is read by the **server** and only affects `select_mcp_tools`.
  Entries are an exact MCP server name or a glob (`git*`, `playwright`),
  case-insensitive. It is seeded with `jev-engine*` so the judge is never a
  candidate for itself — that seed is unconditional and not removable from this
  list.
- `judge_read_prompts` and `inject_agent_instructions` are read by the **plugin
  only**, are **on by default**, and are disabled only by an explicit `false` — a
  typo must not be able to switch the judge off.
- `models` entries **union** non-empty values across files, so a project can add a
  tier without deleting the user's others. An explicit `""` **removes** an
  inherited tier.
- `load_jev_settings()["source"]` is the last contributor; `["sources"]` lists
  every file that contributed. The returned dict is a defensive copy.

### What "routing on" actually does

Only OpenCode, and only with the plugin installed.

When `enable_model_routing` is **on** and at least one tier has a model ID, the
plugin's `chat.message` hook asks Jev the tier, looks up the model ID, and
**forces** the switch by mutating `output.message.model`. A partial `models` map
is fine: if the chosen tier has no ID, nothing is switched and the hook logs why.
When routing is **off** (the default), nothing is changed and the host uses its
own configured or window-selected model.

The two effects have **different** gates, which is the part worth remembering:

| effect | gate | why |
|---|---|---|
| skill injection | `action` is `auto` **or** `review`, and `confidence >= 0.6` | a skill note is advisory text in the user's own message, not an action |
| model switch | `action` is `auto` and `confidence >= 0.6`, plus a well-formed `provider/model` id and an existing `output.message.model` | moving the model changes how the reply is produced, so it takes the server's unreserved verdict |

`escalate` is refused by both — below `JEV_MCP_REVIEW_AT` (0.5) the model is
guessing among options that do not fit.

`select_model_tier` remains available as an MCP tool everywhere, so any harness
can ask for a tier recommendation on demand. Only the plugin can act on it.

---

## Default model (optional)

The top-level `"model"` key is intentionally omitted from
`opencode.example.json`. OpenCode **merges** config files (global
`~/.config/opencode/opencode.json` + project config), so without a `"model"` key
in the project file, the model set in the global config is used automatically. The
`"model"` key is **not** a Jev setting — it only affects what OpenCode uses when
model routing is **off**.

To pin a project-specific default, add it back:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "provider/model-id",   // overrides the global model for this project
  "mcp": { ... }
}
```

> A present `"model"` **overrides** the global config's model — there is no
> fallback to the global value if the ID is invalid. Only set it if you want to pin
> this project to a specific model.

---

## Troubleshooting

Start with `scripts/doctor.py`. If it is clean, the remaining failures are
behavioural.

**`TYPESAFE_API_KEY environment variable is not configured` on every call**
A harness `env` block is setting the key to an empty string, which shadows the
repo `.env`. Remove `TYPESAFE_API_KEY` from the harness config entirely.

**The server does not appear in the harness's server list**
Check the config file location against the table at the top of this page, and
that the placeholder `<REPO_DIR>` was replaced. OpenCode's config schema is strict
(`additionalProperties: false`): an unknown top-level key — a `jev_settings`
block, say — invalidates the **whole** config and the MCP server silently
disappears. Never put Jev settings inside `opencode.json`.

**`CONFIG_ERROR: root_dir is outside the allowed roots`**
The harness is launching the server with a working directory you did not expect.
Add the project root to `JEV_MCP_ALLOWED_ROOTS`. Antigravity needs this by
default.

**VS Code: "Start" button does nothing / server not found**
VS Code uses the `servers` key, **not** `mcpServers`. Copy
`vscode.example.json` verbatim.

**Codex: server configured but never detected**
Check the table name is `mcp_servers` with an underscore, and that the TOML uses
`=` rather than `:`. A TOML syntax error breaks the CLI *and* the IDE extension.

**OpenCode: "Unexpected error occurred" when sending a prompt in a project with a `.agents/` folder**
The `chat.message` hook must never block the OpenCode process. If you have
installed a modified plugin, check that its Jev query is a non-blocking async
stdio round-trip against a long-lived child rather than a synchronous spawn, and
that it **edits** the existing user text part instead of pushing a new one — a
bare `{type:"text", text}` fails OpenCode's `PartV2` validation at save time.
Run `node tests/test_plugin.mjs`; section 13 asserts these properties at the
source level.

**OpenCode: not sure which step failed**
- `~/.config/opencode/logs/jev-plugin.log` — did the hook fire? was the client
  started? per-tool `action`/`confidence`? injected or skipped, and why?
  `no skill to inject: action=auto confidence=0.99 primary=none` means the judge
  answered "nothing here applies", **not** that the plugin failed.
  `skip: N jev queries already in flight` is the concurrency cap;
  `result dropped: superseded by a newer message` is the per-session staleness
  guard. Neither is an error.
- `<repo>/logs/jev_engine.log` — was the tool even invoked? current tool call,
  duration, envelope.
- Reproduce one call over the real transport:
  ```powershell
  & .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "fix ui bug" --root_dir <workspace>
  ```

---

## Security

- **Never commit** your real `opencode.json`, `.mcp.json` or `.env` — they hold
  live API keys. This repository's `.gitignore` blocks `.env`, `opencode.json`,
  `.opencode/`, `.mcp.json`, `.cursor/`, `.codex/`, `.hermes/` and
  `*.local.json`.
- `jevs_settings.json` holds only model IDs (no secrets) — it is safe to commit,
  e.g. to share a team default. The root `.gitignore` does not block it.
- `.env.example` is safe to commit (placeholder only).
- If a real key was ever pushed publicly, treat it as compromised and rotate it.
  See [SECURITY.md](../SECURITY.md).